from rest_framework import serializers

from inventory.models import (
    Category,
    Group,
    Location,
    Part,
    PriceObservation,
    StockEvent,
    Tag,
)


class LocationSerializer(serializers.ModelSerializer):
    class Meta:
        model = Location
        fields = ("code", "name", "notes", "is_active")


class CategorySerializer(serializers.ModelSerializer):
    class Meta:
        model = Category
        fields = ("slug", "name")


class GroupSerializer(serializers.ModelSerializer):
    part_count = serializers.IntegerField(read_only=True)
    # Slug rather than nested, because a consumer rendering a nav has already
    # fetched /categories/ and only needs to know which one this belongs to.
    category = serializers.SlugRelatedField(slug_field="slug", read_only=True)

    class Meta:
        model = Group
        fields = ("slug", "name", "description", "category", "part_count")


class TagSerializer(serializers.ModelSerializer):
    class Meta:
        model = Tag
        fields = ("slug", "name")


class StockEventSerializer(serializers.ModelSerializer):
    part_number = serializers.CharField(source="part.part_number", read_only=True)
    recorded_by = serializers.StringRelatedField(read_only=True)

    class Meta:
        model = StockEvent
        fields = (
            "id",
            "part_number",
            "kind",
            "quantity",
            "observed_at",
            "recorded_at",
            "recorded_by",
            "note",
        )
        read_only_fields = ("id", "recorded_at", "recorded_by")


class PriceObservationSerializer(serializers.ModelSerializer):
    part_number = serializers.CharField(source="part.part_number", read_only=True)
    recorded_by = serializers.StringRelatedField(read_only=True)

    class Meta:
        model = PriceObservation
        fields = (
            "id",
            "part_number",
            "price",
            "currency",
            "supplier",
            "purchase_link",
            "observed_at",
            "recorded_at",
            "recorded_by",
            "note",
        )
        read_only_fields = ("id", "recorded_at", "recorded_by")


class PartSerializer(serializers.ModelSerializer):
    """The read shape ioref-web consumes.

    Stock figures are flattened onto the part because ioref-web's job is to
    answer "can I get one of these right now". It should not have to fetch and
    reduce an event list to render a card.
    """

    location = serializers.SlugRelatedField(
        slug_field="code",
        queryset=Location.objects.all(),
        allow_null=True,
        required=False,
    )
    group = GroupSerializer(read_only=True)
    category = serializers.SlugRelatedField(
        source="effective_category", slug_field="slug", read_only=True
    )
    tags = serializers.SlugRelatedField(slug_field="slug", many=True, read_only=True)
    on_floor = serializers.IntegerField(read_only=True)
    in_backstock = serializers.IntegerField(read_only=True)
    total_on_hand = serializers.IntegerField(read_only=True)
    needs_restock = serializers.BooleanField(read_only=True)
    latest_price = serializers.SerializerMethodField()

    class Meta:
        model = Part
        fields = (
            "part_number",
            "short_name",
            "description",
            "label_text",
            "status",
            "unit",
            "group",
            "category",
            "tags",
            "location",
            "min_quantity",
            "max_quantity",
            "supplier_part_num",
            "manufacturer",
            "manufacturer_part_num",
            "on_floor",
            "in_backstock",
            "total_on_hand",
            "needs_restock",
            "latest_price",
            "updated_at",
        )
        read_only_fields = ("updated_at",)

    def get_latest_price(self, part):
        price = part.latest_price
        if price is None:
            return None
        return {
            "price": str(price.price),
            "currency": price.currency,
            "supplier": price.supplier,
            "purchase_link": price.purchase_link,
            "observed_at": price.observed_at,
        }
