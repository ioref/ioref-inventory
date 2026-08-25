from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import F, OuterRef, Subquery, Value
from django.db.models.functions import Coalesce
from django.utils import timezone


class Location(models.Model):
    """A physical place a part lives.

    First-class rather than a string column, so a shelf does not stop existing
    when the last part leaves it.

    Two naming schemes are in use: grid coordinates like "B1-R1-C1" and free
    names like "soldering bench". `code` is canonical, `name` is the human
    label where there is one.
    """

    code = models.CharField(max_length=64, unique=True)
    name = models.CharField(max_length=128, blank=True)
    notes = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ("code",)

    def __str__(self):
        if self.name and self.code.casefold() == self.name.casefold():
            return self.name
        return f"{self.code} ({self.name})" if self.name else self.code


class Category(models.Model):
    """Where a group sits in the guides' navigation: Input, Output, Power.

    A teaching taxonomy rather than a stock-keeping one, kept here because the
    person who creates a group is the person who knows which category it
    belongs to, and making them edit a second repository to say so is how a
    taxonomy goes stale.

    Rows, not code, so a deployment with no guides site leaves the table empty
    and every group keeps `category = None`. Carries no color and no ordering:
    which categories to show and what they look like is ioref-web's decision,
    and duplicating it here would give two answers to one question.
    """

    name = models.CharField(max_length=100, unique=True)
    slug = models.SlugField(max_length=100, unique=True)

    class Meta:
        ordering = ("name",)
        verbose_name_plural = "categories"

    def __str__(self):
        return self.name


class Group(models.Model):
    """What kind of part this is: Potentiometers, Capacitors, Diodes.

    Flat and singular. A part is one kind of thing, so this is a foreign key
    rather than tags, which is what makes "every capacitor below minimum" and
    "which component page covers this part" unambiguous. Two type-ish tags
    would break both.

    The name stays at the fine level. `Potentiometers` and `Capacitors` would
    mean something to any organization; `Input` only means something to a
    course, so the macro level is a separate field rather than part of the name.
    """

    name = models.CharField(max_length=100, unique=True)
    slug = models.SlugField(max_length=100, unique=True)
    description = models.TextField(blank=True)
    category = models.ForeignKey(
        Category,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="groups",
        help_text="Where this appears in the guides. Blank means it does not.",
    )

    class Meta:
        ordering = ("name",)

    def __str__(self):
        return self.name


class Tag(models.Model):
    """A cross-cutting fact about a part that is not its type.

    Types are singular and exclusive; attributes are not. Part 0386 is a soft
    linear potentiometer whose group is Potentiometers and whose tag is "touch",
    because touch is what it is for. Both are true and only one is what the
    part *is*.

    Also the home for states and uses such as "lending" and "tool box".
    """

    name = models.CharField(max_length=100, unique=True)
    slug = models.SlugField(max_length=100, unique=True)

    class Meta:
        ordering = ("name",)

    def __str__(self):
        return self.name


class PartQuerySet(models.QuerySet):
    """Stock derived in SQL, for the callers that cannot use the properties.

    `on_floor` and `total_on_hand` are Python, so a filter, a sort or a count
    over them would load the table. These express the same definitions as
    subqueries, and the admin, the API and the dashboard all need them.

    The annotations cannot be named `on_floor`/`in_backstock`: those are
    properties, and a read-only property is a data descriptor that setattr(),
    which is how Django applies annotations, cannot write through.
    """

    def with_stock(self):
        return self.annotate(
            _ann_on_floor=_latest_quantity(StockEvent.Kind.INVENTORY),
            _ann_in_backstock=_latest_quantity(StockEvent.Kind.BACKSTOCK),
        )

    def _with_total(self):
        return self.exclude(
            _ann_on_floor__isnull=True, _ann_in_backstock__isnull=True
        ).annotate(
            _ann_total=Coalesce(F("_ann_on_floor"), Value(0))
            + Coalesce(F("_ann_in_backstock"), Value(0))
        )

    def uncounted(self):
        """Neither kind has ever been counted, which is not the same as zero."""
        return self.filter(_ann_on_floor__isnull=True, _ann_in_backstock__isnull=True)

    def below_minimum(self):
        return self._with_total().filter(
            min_quantity__isnull=False, _ann_total__lt=F("min_quantity")
        )

    def out_of_stock(self):
        return self._with_total().filter(_ann_total=0)


def _latest_quantity(kind):
    return Subquery(
        StockEvent.objects.filter(part=OuterRef("pk"), kind=kind)
        .order_by("-observed_at", "-id")
        .values("quantity")[:1]
    )


class Part(models.Model):
    """A stocked item.

    Deliberately excludes guide content. Write-ups and images live in ioref-web
    and join to this table on `part_number`. The teaching category stays here
    with the stock classification, normally through the part's group.
    """

    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        UNAVAILABLE = "unavailable", "Currently Unavailable"
        TO_DISCONTINUE = "to_discontinue", "To Discontinue"
        DISCONTINUED = "discontinued", "Discontinued"

    part_number = models.CharField(max_length=32, unique=True, db_index=True)
    short_name = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    label_text = models.CharField(
        max_length=200, blank=True, help_text="Text printed on the bin label."
    )

    # Not `choices=Status.choices`: staff need to record statuses the four
    # canonical values don't cover ("on backorder", "awaiting quote"). The
    # admin still offers the canonical values as suggestions (see
    # PartAdminForm's status widget) but does not require picking one.
    status = models.CharField(max_length=100, default=Status.ACTIVE, db_index=True)
    unit = models.CharField(max_length=32, default="each")
    group = models.ForeignKey(
        Group,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="parts",
        help_text="What kind of part this is. Independent of where it sits.",
    )
    category = models.ForeignKey(
        Category,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="ungrouped_parts",
        help_text="Guides category for an ungrouped part. Leave blank when a group is set.",
    )
    tags = models.ManyToManyField(
        Tag,
        blank=True,
        related_name="parts",
        help_text="Cross-cutting facts that are not the part's type.",
    )
    location = models.ForeignKey(
        Location,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="parts",
        help_text="Which bin it is in.",
    )

    min_quantity = models.PositiveIntegerField(
        null=True, blank=True, help_text="Restock below this."
    )
    max_quantity = models.PositiveIntegerField(null=True, blank=True)
    supplier_part_num = models.CharField(max_length=128, blank=True)
    manufacturer = models.CharField(max_length=200, blank=True, default="")
    manufacturer_part_num = models.CharField(max_length=128, blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("part_number",)
        constraints = (
            models.CheckConstraint(
                condition=models.Q(group__isnull=True)
                | models.Q(category__isnull=True),
                name="part_category_only_without_group",
            ),
        )

    def __str__(self):
        return f"{self.part_number} {self.short_name}"

    def clean(self):
        super().clean()
        if self.group_id and self.category_id:
            raise ValidationError(
                {"category": "Set a category on the group, not on a grouped part."}
            )

    @property
    def effective_category(self):
        """The group's category, or the direct fallback for an ungrouped part."""
        return self.group.category if self.group_id else self.category

    # ---- Derived stock values -------------------------------------------
    # Never stored. A stored total is a second copy of what the events already
    # say, and the two drift.

    objects = PartQuerySet.as_manager()

    def _latest_quantity(self, kind, annotation):
        """Prefer a queryset annotation, falling back to a per-instance query.

        List endpoints annotate these so the whole page costs a constant number
        of queries. The annotations cannot be named `on_floor`/`in_backstock`
        directly: those are properties, and a read-only property is a data
        descriptor that setattr(), which is how Django applies annotations,
        cannot write through.
        """
        if annotation in self.__dict__:
            return self.__dict__[annotation]
        event = (
            self.stock_events.filter(kind=kind).order_by("-observed_at", "-id").first()
        )
        return event.quantity if event else None

    @property
    def on_floor(self):
        return self._latest_quantity(StockEvent.Kind.INVENTORY, "_ann_on_floor")

    @property
    def in_backstock(self):
        return self._latest_quantity(StockEvent.Kind.BACKSTOCK, "_ann_in_backstock")

    @property
    def total_on_hand(self):
        """Floor + backstock.

        None only when neither has ever been counted. A kind that was never
        counted contributes 0 rather than voiding the total.
        """
        floor, back = self.on_floor, self.in_backstock
        if floor is None and back is None:
            return None
        return (floor or 0) + (back or 0)

    @property
    def stock_ratio(self):
        """How much is on hand as a fraction of the minimum."""
        total = self.total_on_hand
        if total is None or not self.min_quantity:
            return None
        return Decimal(total) / Decimal(self.min_quantity)

    @property
    def needs_restock(self):
        total = self.total_on_hand
        if total is None or self.min_quantity is None:
            return False
        return total < self.min_quantity

    @property
    def latest_price(self):
        # Use the prefetched list when there is one; calling .order_by() on the
        # related manager issues a fresh query and defeats the prefetch. The
        # model's Meta.ordering already puts the newest first.
        if "price_observations" in getattr(self, "_prefetched_objects_cache", {}):
            observations = self.price_observations.all()
            return observations[0] if observations else None
        return self.price_observations.order_by("-observed_at", "-id").first()


class StockEventQuerySet(models.QuerySet):
    def latest_per_part(self, kind):
        """One row per part: its most recent event of `kind`.

        Lets list endpoints avoid an N+1 over the property accessors above.
        """
        newest = (
            StockEvent.objects.filter(part=OuterRef("part"), kind=kind)
            .order_by("-observed_at", "-id")
            .values("id")[:1]
        )
        return self.filter(kind=kind, id__in=Subquery(newest))


class StockEvent(models.Model):
    """An append-only observation of how many of a part were counted.

    Rows are never updated or deleted. A miscount is corrected by recording a
    new observation, so the audit trail stays intact.
    """

    class Kind(models.TextChoices):
        INVENTORY = "inventory", "On floor"
        BACKSTOCK = "backstock", "Backstock"

    part = models.ForeignKey(
        Part, on_delete=models.CASCADE, related_name="stock_events"
    )
    kind = models.CharField(max_length=16, choices=Kind.choices)
    quantity = models.PositiveIntegerField()

    observed_at = models.DateTimeField(
        default=timezone.now, help_text="When the count was taken."
    )
    recorded_at = models.DateTimeField(auto_now_add=True)
    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="stock_events",
    )
    note = models.TextField(blank=True)

    objects = StockEventQuerySet.as_manager()

    class Meta:
        ordering = ("-observed_at", "-id")
        indexes = [models.Index(fields=["part", "kind", "-observed_at"])]

    def __str__(self):
        return f"{self.part.part_number} {self.kind}={self.quantity} @ {self.observed_at:%Y-%m-%d}"


class PriceObservation(models.Model):
    """An append-only record of what a part cost, from whom, and where to rebuy.

    Price, supplier and link are one row, so a price is always attributable to
    whoever sold it at that price.
    """

    part = models.ForeignKey(
        Part, on_delete=models.CASCADE, related_name="price_observations"
    )
    # 5dp preserves low unit prices from bulk purchases, such as $0.01087.
    price = models.DecimalField(max_digits=12, decimal_places=5)
    currency = models.CharField(max_length=3, default="USD")
    supplier = models.CharField(max_length=200, blank=True)
    purchase_link = models.URLField(max_length=1000, blank=True)

    observed_at = models.DateTimeField(
        default=timezone.now, help_text="When the price was observed."
    )
    recorded_at = models.DateTimeField(auto_now_add=True)
    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="price_observations",
    )
    note = models.TextField(blank=True)

    class Meta:
        ordering = ("-observed_at", "-id")
        indexes = [models.Index(fields=["part", "-observed_at"])]

    def __str__(self):
        return f"{self.part.part_number} {self.currency} {self.price} @ {self.observed_at:%Y-%m-%d}"
