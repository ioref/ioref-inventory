from urllib.parse import urlparse

from django import forms
from django.contrib import admin
from django.db.models import Count
from django.shortcuts import redirect
from django.urls import reverse
from django.utils.html import format_html
from django.utils.http import urlencode
from unfold.admin import ModelAdmin, TabularInline
from unfold.contrib.filters.admin import RangeDateFilter
from unfold.decorators import action
from unfold.widgets import UnfoldAdminSelect2Widget

from .models import (
    Category,
    Group,
    Location,
    Part,
    PriceObservation,
    StockEvent,
    Tag,
)


class PartCountColumn:
    """A parts count that opens the part list already filtered to that row.

    Counting the reverse relation per row costs one query per row, because
    list_display calls the display method once per object. The count is
    annotated instead, under the `_ann_` prefix the rest of this module uses.

    The lookup each subclass names has to be one PartAdmin will accept: the
    changelist rejects a filter that is not in its list_filter, so `group`,
    `tags` and `location` work and anything else would 400.
    """

    #: Query parameter on the part changelist that filters to one of these.
    parts_lookup = None

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(_ann_parts=Count("parts"))

    @admin.display(description="Parts", ordering="_ann_parts")
    def part_count(self, obj):
        count = obj._ann_parts
        if not count:
            # An empty group is a real state worth seeing, but a link to an
            # empty list is a dead end, so it stays plain text.
            return count
        url = reverse("admin:inventory_part_changelist")
        query = urlencode({self.parts_lookup: obj.pk})
        return format_html('<a href="{}?{}">{}</a>', url, query, count)


class StockStateFilter(admin.SimpleListFilter):
    """The three states the stock column marks, as a filter.

    Defined against the `_ann_` annotations PartAdmin already adds rather than
    the model properties, because a filter has to be SQL: `needs_restock` and
    `total_on_hand` are Python and cannot reach the database.

    The definitions match the column exactly, which matters because the
    dashboard counts link here. Never counted means neither kind has an event,
    not that the total is zero.
    """

    title = "stock state"
    parameter_name = "stock"

    def lookups(self, request, model_admin):
        return (
            ("low", "Below minimum"),
            ("zero", "Out of stock"),
            ("uncounted", "Never counted"),
        )

    def queryset(self, request, queryset):
        return {
            "low": queryset.below_minimum,
            "zero": queryset.out_of_stock,
            "uncounted": queryset.uncounted,
        }.get(self.value(), lambda: queryset)()


class StockEventInline(TabularInline):
    model = StockEvent
    extra = 0
    fields = ("kind", "quantity", "observed_at", "recorded_by", "note")
    readonly_fields = ("recorded_by",)
    ordering = ("-observed_at",)
    # History is append-only: corrections are made by adding a newer count, so
    # the record of what was believed and when survives.
    can_delete = False

    def has_change_permission(self, request, obj=None):
        return False


class PriceObservationInline(TabularInline):
    model = PriceObservation
    extra = 0
    show_title = False
    fields = (
        "price",
        "currency",
        "supplier",
        "purchase_link",
        "purchase",
        "observed_at",
        "note",
    )
    readonly_fields = ("purchase",)
    ordering = ("-observed_at",)
    can_delete = False

    def has_change_permission(self, request, obj=None):
        return False

    @admin.display(description="Open link")
    def purchase(self, obj):
        """The supplier's page, opened in a new tab.

        A readonly URLField renders as plain text, so this was previously an
        un-clickable string. New tab because the link leaves for a supplier
        site and whoever followed it is part-way through counting something.
        """
        if not obj.purchase_link:
            return ""
        return format_html(
            '<a href="{}" target="_blank" rel="noopener noreferrer">{}</a>',
            obj.purchase_link,
            urlparse(obj.purchase_link).netloc or "link",
        )


class PartAdminForm(forms.ModelForm):
    status = forms.CharField(
        widget=UnfoldAdminSelect2Widget(
            attrs={
                "data-placeholder": "Select or enter a status",
                "data-tags": "true",
            }
        )
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        statuses = {
            value.casefold(): (value, label) for value, label in Part.Status.choices
        }
        for value in (
            Part.objects.exclude(status="").values_list("status", flat=True).distinct()
        ):
            statuses.setdefault(value.casefold(), (value, value))

        current = getattr(self.instance, "status", "")
        if current:
            statuses.setdefault(current.casefold(), (current, current))

        self.fields["status"].widget.choices = sorted(
            statuses.values(), key=lambda choice: choice[1].casefold()
        )

    class Meta:
        model = Part
        fields = "__all__"


@admin.register(Part)
class PartAdmin(ModelAdmin):
    form = PartAdminForm

    # Rendered at the top of the change form. The inlines below are newest
    # first and unbounded, so the "add another" link at the foot of a
    # well-counted part is a long way down a page nobody wants to scroll
    # while holding a barcode scanner.
    actions_detail = ["record_count", "record_price"]

    list_display = (
        "part_number",
        "short_name",
        "group",
        "status",
        "location",
        "on_floor",
        "in_backstock",
        "stock_state",
    )
    list_filter = (
        StockStateFilter,
        "status",
        "group",
        "category",
        "tags",
        "location",
    )
    search_fields = (
        "part_number",
        "short_name",
        "description",
        "supplier_part_num",
        "manufacturer",
        "manufacturer_part_num",
    )
    autocomplete_fields = ("group", "category", "tags")
    inlines = (StockEventInline, PriceObservationInline)
    list_select_related = ("location", "group", "category")

    @action(description="Record count", icon="add", url_path="record-count")
    def record_count(self, request, object_id):
        return self._add_for_part(StockEvent, object_id)

    @action(description="Record price", icon="payments", url_path="record-price")
    def record_price(self, request, object_id):
        return self._add_for_part(PriceObservation, object_id)

    def _add_for_part(self, model, object_id):
        """Open the child's add form with the part already chosen.

        Django's add view fills a field from a query parameter of the same
        name, so this needs no custom form. `_part` carries the same value
        back out again, which is what tells response_add where to return to.
        """
        meta = model._meta
        url = reverse(f"admin:{meta.app_label}_{meta.model_name}_add")
        return redirect(f"{url}?part={object_id}&_part={object_id}")

    def has_delete_permission(self, request, obj=None):
        """A part is never deleted, by anyone.

        StockEvent and PriceObservation cascade from Part, so deleting one row
        here silently takes every count and price ever recorded against it. The
        append-only history exists precisely so that a correction never
        destroys what it corrects, and a delete button undoes that in a click.

        Retiring a part is a status change: TO_DISCONTINUE, then DISCONTINUED.
        The bin keeps its label and the history stays answerable.

        Returning False also withdraws the bulk "delete selected" action, which
        is the more dangerous of the two.
        """
        return False

    def get_queryset(self, request):
        # Without these annotations the on_floor/in_backstock/stock_state
        # columns each hit the database per row, so a 100-row page costs ~200
        # extra queries. The properties read the annotations back when present.
        return super().get_queryset(request).with_stock()

    @admin.display(description="Stock", ordering="_ann_on_floor")
    def stock_state(self, part):
        """Total on hand, flagged only when it needs attention.

        A healthy row is just a number. Marking every row, including the ~90%
        that are fine, makes the list harder to scan rather than easier, and
        buries the handful that actually need restocking.
        """
        total = part.total_on_hand

        if total is None:
            return format_html(
                '<span class="stock-flag stock-flag--unknown" data-flag="uncounted"'
                ' title="No count has ever been recorded">—</span>'
            )

        if part.needs_restock:
            return format_html(
                '<span class="stock-flag stock-flag--restock" data-flag="low"'
                ' title="Below the minimum of {}">{}</span>',
                part.min_quantity,
                total,
            )

        return format_html('<span class="stock-value">{}</span>', total)

    class Media:
        css = {"all": ("inventory/admin.css",)}
        # Widens the object link's hit area to the whole row.
        js = ("inventory/admin.js",)

    def save_formset(self, request, form, formset, change):
        # Attribute counts entered through the admin to whoever typed them.
        instances = formset.save(commit=False)
        for instance in instances:
            if isinstance(instance, (StockEvent, PriceObservation)) and not instance.pk:
                instance.recorded_by = request.user
            instance.save()
        formset.save_m2m()


@admin.register(Location)
class LocationAdmin(PartCountColumn, ModelAdmin):
    parts_lookup = "location__id__exact"
    list_display = ("code", "name", "is_active", "part_count")
    list_filter = ("is_active",)
    search_fields = ("code", "name")


class AttributesToRecorder:
    """Default recorded_by to whoever is filling the form in.

    PartAdmin.save_formset already attributes rows added through the inlines.
    These are the same records reached through their own add form, which is
    where the "Record count" button sends people, so without this the history
    loses who counted.

    Offered as an initial value rather than forced in save_model, so someone
    entering a backlog of counts taken on paper can still say whose they were.
    """

    def get_changeform_initial_data(self, request):
        initial = super().get_changeform_initial_data(request)
        initial.setdefault("recorded_by", request.user.pk)
        return initial


class ReturnsToPart:
    """Send the user back to the part they came from after adding a record.

    The add form posts to its own URL, so the `_part` parameter that
    PartAdmin._add_for_part put there survives the round trip. Without this a
    count recorded from a part page lands on the stock event changelist, which
    is not where anybody was looking.
    """

    def response_add(self, request, obj, post_url_continue=None):
        part_id = request.GET.get("_part")
        if part_id and "_addanother" not in request.POST:
            return redirect(reverse("admin:inventory_part_change", args=[part_id]))
        return super().response_add(request, obj, post_url_continue)


@admin.register(StockEvent)
class StockEventAdmin(AttributesToRecorder, ReturnsToPart, ModelAdmin):
    list_display = ("part", "kind", "quantity", "observed_at", "recorded_by")
    list_filter = ("kind", ("observed_at", RangeDateFilter))
    search_fields = ("part__part_number", "part__short_name")
    date_hierarchy = "observed_at"
    list_select_related = ("part", "recorded_by")


@admin.register(PriceObservation)
class PriceObservationAdmin(AttributesToRecorder, ReturnsToPart, ModelAdmin):
    list_display = ("part", "price", "currency", "supplier", "observed_at")
    list_filter = ("currency", "supplier")
    search_fields = ("part__part_number", "part__short_name", "supplier")
    date_hierarchy = "observed_at"
    list_select_related = ("part",)


@admin.register(Category)
class CategoryAdmin(ModelAdmin):
    """The macro taxonomy. ioref-web decides which of these it shows."""

    list_display = ("name", "slug", "group_count")
    search_fields = ("name", "slug")
    prepopulated_fields = {"slug": ("name",)}

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(_ann_groups=Count("groups"))

    @admin.display(description="Groups", ordering="_ann_groups")
    def group_count(self, obj):
        if not obj._ann_groups:
            return obj._ann_groups
        url = reverse("admin:inventory_group_changelist")
        query = urlencode({"category__id__exact": obj.pk})
        return format_html('<a href="{}?{}">{}</a>', url, query, obj._ann_groups)


@admin.register(Group)
class GroupAdmin(PartCountColumn, ModelAdmin):
    parts_lookup = "group__id__exact"
    list_display = ("name", "slug", "category", "part_count")
    list_filter = ("category",)
    list_select_related = ("category",)
    search_fields = ("name", "slug")
    prepopulated_fields = {"slug": ("name",)}


@admin.register(Tag)
class TagAdmin(PartCountColumn, ModelAdmin):
    parts_lookup = "tags__id__exact"
    list_display = ("name", "slug", "part_count")
    search_fields = ("name", "slug")
    prepopulated_fields = {"slug": ("name",)}
