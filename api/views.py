from django.db.models import Count, F, OuterRef, Prefetch, Q, Subquery, Value
from django.db.models.functions import Coalesce
from django.utils import timezone
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from accounts.authentication import ApiKeyUser
from inventory.models import (
    Category,
    Group,
    Location,
    Part,
    PriceObservation,
    StockEvent,
    Tag,
)

from .serializers import (
    CategorySerializer,
    GroupSerializer,
    LocationSerializer,
    PartSerializer,
    PriceObservationSerializer,
    StockEventSerializer,
    TagSerializer,
)


def _latest_quantity_subquery(kind):
    return Subquery(
        StockEvent.objects.filter(part=OuterRef("pk"), kind=kind)
        .order_by("-observed_at", "-id")
        .values("quantity")[:1]
    )


class PartViewSet(viewsets.ModelViewSet):
    """Parts, addressed by part_number rather than surrogate id.

    part_number is the key ioref-web already holds on its PartPage, so
    exposing it as the URL segment means ioref-web never has to keep a
    mapping table of our primary keys.
    """

    serializer_class = PartSerializer
    lookup_field = "part_number"
    lookup_value_regex = "[^/]+"

    def get_queryset(self):
        # Annotating the latest counts keeps list responses to a constant number
        # of queries; the model properties would issue two per part.
        # The _ann_ prefix is required: Part.on_floor/.in_backstock are read-only
        # properties, and Django applies annotations with setattr(), which cannot
        # write through a data descriptor. The properties read these back.
        queryset = (
            Part.objects.select_related("location", "group__category", "category")
            .prefetch_related("tags")
            .annotate(
                _ann_on_floor=_latest_quantity_subquery(StockEvent.Kind.INVENTORY),
                _ann_in_backstock=_latest_quantity_subquery(StockEvent.Kind.BACKSTOCK),
            )
            .prefetch_related(
                Prefetch(
                    "price_observations",
                    queryset=PriceObservation.objects.order_by("-observed_at", "-id"),
                )
            )
        )

        params = self.request.query_params

        if (status_filter := params.get("status")) is not None:
            queryset = queryset.filter(status=status_filter)
        if (location := params.get("location")) is not None:
            queryset = queryset.filter(location__code=location)
        if (group := params.get("group")) is not None:
            queryset = queryset.filter(group__slug=group)
        if params.get("ungrouped") in ("1", "true", "yes"):
            queryset = queryset.filter(group__isnull=True)
        if (category := params.get("category")) is not None:
            queryset = queryset.filter(
                Q(group__category__slug=category)
                | Q(group__isnull=True, category__slug=category)
            )
        if (tag := params.get("tag")) is not None:
            queryset = queryset.filter(tags__slug=tag)
        if (search := params.get("search")) is not None:
            queryset = queryset.filter(short_name__icontains=search)

        if (numbers := params.get("part_number__in")) is not None:
            # Bulk lookup for consumers that group several stocked parts under
            # one heading. ioref-web's component pages do, and the
            # ceramic capacitors run to 33 part numbers. Without this the page
            # would issue one request per part.
            wanted = [n.strip() for n in numbers.split(",") if n.strip()]
            queryset = queryset.filter(part_number__in=wanted)

        if params.get("needs_restock") in ("1", "true", "yes"):
            # Kept in SQL rather than filtering in Python so pagination stays
            # correct and the whole table is not loaded to answer the question.
            queryset = queryset.annotate(
                _total=Coalesce(F("_ann_on_floor"), Value(0))
                + Coalesce(F("_ann_in_backstock"), Value(0))
            ).filter(min_quantity__isnull=False, _total__lt=F("min_quantity"))

        return queryset

    def _record(self, request, part, serializer_class, **extra):
        serializer = serializer_class(data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save(
            part=part,
            # API-key callers are not people, so recorded_by stays null and the
            # key's identity is preserved in the note instead.
            recorded_by=None if isinstance(request.user, ApiKeyUser) else request.user,
            **extra,
        )
        return Response(serializer.data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["get", "post"], url_path="stock-events")
    def stock_events(self, request, part_number=None):
        part = self.get_object()
        if request.method == "POST":
            return self._record(request, part, StockEventSerializer)
        events = part.stock_events.select_related("recorded_by")
        if (kind := request.query_params.get("kind")) is not None:
            events = events.filter(kind=kind)
        page = self.paginate_queryset(events)
        return self.get_paginated_response(StockEventSerializer(page, many=True).data)

    @action(detail=True, methods=["get", "post"], url_path="prices")
    def prices(self, request, part_number=None):
        part = self.get_object()
        if request.method == "POST":
            return self._record(request, part, PriceObservationSerializer)
        observations = part.price_observations.select_related("recorded_by")
        page = self.paginate_queryset(observations)
        return self.get_paginated_response(
            PriceObservationSerializer(page, many=True).data
        )


class LocationViewSet(viewsets.ModelViewSet):
    queryset = Location.objects.all()
    serializer_class = LocationSerializer
    lookup_field = "code"
    lookup_value_regex = "[^/]+"


class CategoryViewSet(viewsets.ReadOnlyModelViewSet):
    """The macro taxonomy. ioref-web decides which of these to show."""

    queryset = Category.objects.all()
    serializer_class = CategorySerializer
    lookup_field = "slug"


class GroupViewSet(viewsets.ReadOnlyModelViewSet):
    """Part kinds. ioref-web builds component pages from these."""

    serializer_class = GroupSerializer
    lookup_field = "slug"

    def get_queryset(self):
        # part_count saves ioref-web a request per group when it is deciding
        # which groups are worth a component page.
        queryset = Group.objects.select_related("category").annotate(
            part_count=Count("parts")
        )
        if (category := self.request.query_params.get("category")) is not None:
            queryset = queryset.filter(category__slug=category)
        return queryset


class TagViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = Tag.objects.all()
    serializer_class = TagSerializer
    lookup_field = "slug"


class HealthViewSet(mixins.ListModelMixin, viewsets.GenericViewSet):
    """Unauthenticated liveness probe for container orchestration."""

    authentication_classes = []
    permission_classes = []

    def list(self, request):
        return Response({"status": "ok", "time": timezone.now()})
