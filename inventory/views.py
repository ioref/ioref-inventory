"""Public read-only browsing of the inventory.

This exists so the application is useful on its own. ioref-web renders a
branded version of the same data over the API, but a deployment with nothing in
front of it, another organization running this by itself, still needs a way
to look at its own stock.

Deliberately unstyled in anyone's house colors. CMU-specific presentation
belongs in ioref-web; this is neutral so it does not look wrong elsewhere.

Read-only and safe for anonymous access, with one exception: prices and
suppliers are shown only to signed-in staff. Stock levels answer "do you have
any, and where"; what it cost and who sold it is procurement's business.
"""

from django.conf import settings
from django.db.models import OuterRef, Subquery
from django.http import Http404
from django.shortcuts import get_object_or_404, render

from .models import Part, StockEvent


def _latest_quantity(kind):
    return Subquery(
        StockEvent.objects.filter(part=OuterRef("pk"), kind=kind)
        .order_by("-observed_at", "-id")
        .values("quantity")[:1]
    )


def _browsable():
    if not settings.PUBLIC_BROWSE:
        raise Http404("Public browsing is disabled on this deployment.")


def _may_see_costs(request):
    return request.user.is_authenticated


def part_list(request):
    _browsable()

    # See CLAUDE.md: the annotations cannot be named on_floor/in_backstock,
    # which are read-only properties that setattr() cannot write through.
    parts = (
        Part.objects.annotate(
            _ann_on_floor=_latest_quantity(StockEvent.Kind.INVENTORY),
            _ann_in_backstock=_latest_quantity(StockEvent.Kind.BACKSTOCK),
        )
        .order_by("part_number")
    )

    return render(
        request,
        "inventory/part_list.html",
        {
            "parts": parts,
            "total": parts.count(),
            "may_see_costs": _may_see_costs(request),
        },
    )


def part_detail(request, part_number):
    _browsable()

    part = get_object_or_404(
        Part.objects.select_related("location", "group").prefetch_related("tags"),
        part_number=part_number,
    )
    may_see_costs = _may_see_costs(request)

    return render(
        request,
        "inventory/part_detail.html",
        {
            "part": part,
            "counts": part.stock_events.select_related("recorded_by")[:20],
            # Querying prices at all is skipped for anonymous visitors rather
            # than fetched and hidden in the template.
            "prices": part.price_observations.all()[:10] if may_see_costs else None,
            "may_see_costs": may_see_costs,
        },
    )
