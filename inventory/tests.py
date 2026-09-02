from datetime import timedelta

from django.contrib.admin import site
from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import ApiKey
from inventory.admin import PartAdminForm
from inventory.models import (
    Category,
    Group,
    Location,
    Part,
    PriceObservation,
    StockEvent,
    Tag,
)


class PartStatusFormTests(TestCase):
    def test_status_combobox_lists_canonical_and_existing_values(self):
        Part.objects.create(
            part_number="0001", short_name="Test part", status="awaiting_quote"
        )

        field = PartAdminForm().fields["status"]
        choices = dict(field.widget.choices)

        self.assertEqual(field.widget.attrs["data-tags"], "true")
        self.assertIn(Part.Status.ACTIVE, choices)
        self.assertIn("awaiting_quote", choices)

    def test_status_field_accepts_a_new_value(self):
        field = PartAdminForm().fields["status"]

        self.assertEqual(field.clean("on_backorder"), "on_backorder")


class LocationLabelTests(TestCase):
    def test_case_only_code_and_name_are_not_repeated(self):
        location = Location(code="soldering bench", name="Soldering bench")

        self.assertEqual(str(location), "Soldering bench")

    def test_distinct_code_and_name_are_both_shown(self):
        location = Location(code="B1-R1-C1", name="Resistors")

        self.assertEqual(str(location), "B1-R1-C1 (Resistors)")


class StockDerivationTests(TestCase):
    """Stock derived from events rather than stored."""

    def setUp(self):
        self.bench = Location.objects.create(code="soldering bench")
        self.part = Part.objects.create(
            part_number="0002",
            short_name="soldering heat sink",
            location=self.bench,
            min_quantity=2,
            unit="each",
        )

    def _count(self, kind, quantity, days_ago=0):
        return StockEvent.objects.create(
            part=self.part,
            kind=kind,
            quantity=quantity,
            observed_at=timezone.now() - timedelta(days=days_ago),
        )

    def test_uncounted_part_reports_none_not_zero(self):
        # Never counted is not zero. Absence has to stay distinguishable from
        # a real zero, or restocking decisions are made on invented numbers.
        self.assertIsNone(self.part.on_floor)
        self.assertIsNone(self.part.total_on_hand)
        self.assertFalse(self.part.needs_restock)

    def test_latest_observation_wins_regardless_of_insert_order(self):
        self._count(StockEvent.Kind.INVENTORY, 5, days_ago=1)
        self._count(StockEvent.Kind.INVENTORY, 99, days_ago=30)  # backdated
        self.assertEqual(self.part.on_floor, 5)

    def test_total_is_floor_plus_backstock(self):
        # Part 2409: inventory 1 + backstock 10 = 11 on hand,
        # and 11/3 rendered as "367%".
        part = Part.objects.create(
            part_number="2409", short_name="brass wool", min_quantity=3
        )
        now = timezone.now()
        StockEvent.objects.create(
            part=part, kind=StockEvent.Kind.INVENTORY, quantity=1, observed_at=now
        )
        StockEvent.objects.create(
            part=part, kind=StockEvent.Kind.BACKSTOCK, quantity=10, observed_at=now
        )
        self.assertEqual(part.total_on_hand, 11)
        self.assertAlmostEqual(float(part.stock_ratio), 11 / 3, places=4)

    def test_one_kind_counted_still_totals(self):
        self._count(StockEvent.Kind.INVENTORY, 4)
        self.assertEqual(self.part.total_on_hand, 4)

    def test_needs_restock_below_minimum(self):
        self._count(StockEvent.Kind.INVENTORY, 1)
        self.assertTrue(self.part.needs_restock)
        self._count(StockEvent.Kind.BACKSTOCK, 5)
        self.assertFalse(self.part.needs_restock)


class ApiKeyTests(TestCase):
    def test_generated_token_authenticates_and_is_not_stored(self):
        key, token = ApiKey.generate("ioref-web")
        self.assertNotIn(token, key.hashed_key)
        self.assertEqual(ApiKey.authenticate(token), key)

    def test_wrong_and_malformed_tokens_rejected(self):
        _, token = ApiKey.generate("ioref-web")
        self.assertIsNone(ApiKey.authenticate(token + "x"))
        self.assertIsNone(ApiKey.authenticate("garbage"))
        self.assertIsNone(ApiKey.authenticate(""))

    def test_revoked_and_expired_keys_rejected(self):
        key, token = ApiKey.generate("revoked")
        key.is_active = False
        key.save()
        self.assertIsNone(ApiKey.authenticate(token))

        expired, token2 = ApiKey.generate("expired")
        expired.expires_at = timezone.now() - timedelta(seconds=1)
        expired.save()
        self.assertIsNone(ApiKey.authenticate(token2))


class ApiTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.part = Part.objects.create(
            part_number="0006", short_name="soldering sponge", min_quantity=5
        )
        StockEvent.objects.create(
            part=self.part,
            kind=StockEvent.Kind.INVENTORY,
            quantity=7,
            observed_at=timezone.now(),
        )
        _, self.read_token = ApiKey.generate("ioref-web", scope=ApiKey.Scope.READ)
        _, self.write_token = ApiKey.generate("scanner", scope=ApiKey.Scope.WRITE)

    def auth(self, token):
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")

    def test_anonymous_is_denied(self):
        self.assertEqual(self.client.get("/api/v1/parts/").status_code, 401)

    def test_health_needs_no_auth(self):
        self.assertEqual(self.client.get("/api/v1/health/").status_code, 200)

    def test_read_key_gets_flattened_stock(self):
        self.auth(self.read_token)
        response = self.client.get(f"/api/v1/parts/{self.part.part_number}/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["on_floor"], 7)
        self.assertEqual(response.data["total_on_hand"], 7)

    def test_list_annotation_path_works(self):
        """Exercises the _ann_ annotations rather than the property fallback."""
        self.auth(self.read_token)
        response = self.client.get("/api/v1/parts/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["results"][0]["on_floor"], 7)

    def test_needs_restock_filter_runs_in_sql(self):
        self.auth(self.read_token)
        response = self.client.get("/api/v1/parts/?needs_restock=1")
        # 7 on hand against a minimum of 5, so not due a restock.
        self.assertEqual(response.data["count"], 0)

        StockEvent.objects.create(
            part=self.part,
            kind=StockEvent.Kind.INVENTORY,
            quantity=2,
            observed_at=timezone.now(),
        )
        response = self.client.get("/api/v1/parts/?needs_restock=1")
        self.assertEqual(response.data["count"], 1)

    def test_read_key_cannot_write(self):
        self.auth(self.read_token)
        response = self.client.post(
            f"/api/v1/parts/{self.part.part_number}/stock-events/",
            {"kind": "inventory", "quantity": 3, "observed_at": timezone.now()},
            format="json",
        )
        self.assertEqual(response.status_code, 403)

    def test_write_key_records_event_without_a_user(self):
        self.auth(self.write_token)
        response = self.client.post(
            f"/api/v1/parts/{self.part.part_number}/stock-events/",
            {"kind": "inventory", "quantity": 3, "observed_at": timezone.now()},
            format="json",
        )
        self.assertEqual(response.status_code, 201)
        event = self.part.stock_events.order_by("-recorded_at").first()
        self.assertEqual(event.quantity, 3)
        # Service callers are not people; nothing should be attributed to a user.
        self.assertIsNone(event.recorded_by)

    def test_history_is_queryable(self):
        """The thing the JSON blobs made impossible without loading everything."""
        self.auth(self.read_token)
        response = self.client.get(
            f"/api/v1/parts/{self.part.part_number}/stock-events/?kind=inventory"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["count"], 1)


class BulkLookupTests(TestCase):
    """`part_number__in`, used by ioref-web's component pages."""

    def setUp(self):
        self.client = APIClient()
        for number in ("0054", "0056", "0058"):
            Part.objects.create(part_number=number, short_name=f"cap {number}")
        _, token = ApiKey.generate("web", scope=ApiKey.Scope.READ)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")

    def test_returns_only_the_requested_parts(self):
        response = self.client.get("/api/v1/parts/?part_number__in=0054,0058")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            sorted(p["part_number"] for p in response.data["results"]), ["0054", "0058"]
        )

    def test_tolerates_whitespace_and_unknown_numbers(self):
        response = self.client.get("/api/v1/parts/?part_number__in= 0054 , 9999 ")
        self.assertEqual([p["part_number"] for p in response.data["results"]], ["0054"])

    def test_one_request_covers_many_parts(self):
        response = self.client.get("/api/v1/parts/?part_number__in=0054,0056,0058")
        self.assertEqual(response.data["count"], 3)


class PublicBrowseTests(TestCase):
    """Read-only HTML views at /.

    The security-relevant behavior is that anonymous visitors see stock but
    not what it cost or who sold it.
    """

    def setUp(self):
        self.bench = Location.objects.create(code="soldering bench")
        self.part = Part.objects.create(
            part_number="0002",
            short_name="soldering heat sink",
            location=self.bench,
            min_quantity=2,
        )
        StockEvent.objects.create(
            part=self.part,
            kind=StockEvent.Kind.INVENTORY,
            quantity=5,
            observed_at=timezone.now(),
        )
        PriceObservation.objects.create(
            part=self.part,
            price="2.20",
            supplier="Amazon",
            purchase_link="https://example.invalid/thing",
            observed_at=timezone.now(),
        )

    def test_list_is_public(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "soldering heat sink")

    def test_detail_is_public_and_shows_stock(self):
        response = self.client.get("/parts/0002/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "5")

    def test_anonymous_visitors_do_not_see_prices_or_suppliers(self):
        response = self.client.get("/parts/0002/")
        self.assertNotContains(response, "Amazon")
        self.assertNotContains(response, "2.20")
        self.assertNotContains(response, "example.invalid")

    def test_signed_in_staff_do_see_prices(self):
        User = get_user_model()
        user = User.objects.create_user(username="staff@example.edu", password="x")
        self.client.force_login(user)
        response = self.client.get("/parts/0002/")
        self.assertContains(response, "Amazon")
        self.assertContains(response, "2.20")

    def test_a_categorized_group_shows_its_category_badge(self):
        """category-<slug> is the exact class ioref-web's main.scss keys off,
        so the two sites read the same badge identically. Nothing exercised
        this path before: no group had a category assigned when it was
        written, so a typo in the slug or the template conditional would have
        shipped silently."""
        power = Category.objects.get(slug="power")
        resistors = Group.objects.create(
            name="Resistors", slug="resistor", category=power
        )
        self.part.group = resistors
        self.part.save()

        # Category badge only shown on detail page, not in spreadsheet list view
        response = self.client.get("/parts/0002/")
        self.assertContains(response, 'class="category category-power"')
        self.assertContains(response, "Power")

    def test_an_uncategorized_group_shows_no_badge(self):
        """The common case today: Category exists but is not yet populated
        for most groups, and that must render as "nothing", not an error."""
        self.part.group = Group.objects.create(name="Resistors", slug="resistor")
        self.part.save()

        for url in ("/", "/parts/0002/"):
            response = self.client.get(url)
            self.assertNotContains(response, "category category-")

    def test_all_parts_returned_for_client_side_filtering(self):
        # Client-side filtering means all parts are returned in the HTML
        Part.objects.create(part_number="0099", short_name="brass wool")
        response = self.client.get("/")
        self.assertContains(response, "brass wool")
        self.assertContains(response, "soldering heat sink")

    def test_discontinued_parts_included_for_client_side_toggle(self):
        # Discontinued parts are now shown with client-side checkbox toggle
        Part.objects.create(
            part_number="0028",
            short_name="retired thing",
            status=Part.Status.DISCONTINUED,
        )
        response = self.client.get("/")
        self.assertContains(response, "retired thing")

    def test_unknown_part_404s(self):
        self.assertEqual(self.client.get("/parts/9999/").status_code, 404)

    @override_settings(PUBLIC_BROWSE=False)
    def test_browsing_can_be_switched_off(self):
        """Deployments where stock levels themselves are not public."""
        self.assertEqual(self.client.get("/").status_code, 404)
        self.assertEqual(self.client.get("/parts/0002/").status_code, 404)

    @override_settings(PUBLIC_BROWSE=False)
    def test_switching_browsing_off_does_not_affect_the_api(self):
        _, token = ApiKey.generate("web", scope=ApiKey.Scope.READ)
        response = self.client.get(
            "/api/v1/parts/", HTTP_AUTHORIZATION=f"Bearer {token}"
        )
        self.assertEqual(response.status_code, 200)


class GroupTests(TestCase):
    """Classification, kept separate from location.

    Separate fields, so a part can be reclassified without appearing to move
    and moved without appearing to be reclassified.
    """

    def setUp(self):
        self.pots = Group.objects.create(name="Potentiometers", slug="potentiometers")
        self.bin = Location.objects.create(code="B11-R2-C3")
        self.part = Part.objects.create(
            part_number="0390",
            short_name="potentiometer",
            group=self.pots,
            location=self.bin,
        )

    def test_a_part_can_move_bins_without_being_reclassified(self):
        self.part.location = Location.objects.create(code="B12-R1-C1")
        self.part.save()
        self.part.refresh_from_db()
        self.assertEqual(self.part.group, self.pots)

    def test_a_part_can_be_reclassified_without_moving(self):
        trimmers = Group.objects.create(name="Trimmers", slug="trimmers")
        self.part.group = trimmers
        self.part.save()
        self.part.refresh_from_db()
        self.assertEqual(self.part.location, self.bin)

    def test_slugs_are_unique(self):
        from django.db import IntegrityError

        with self.assertRaises(IntegrityError):
            Group.objects.create(name="Pots", slug="potentiometers")

    def test_group_in_use_cannot_be_deleted(self):
        from django.db.models import ProtectedError

        with self.assertRaises(ProtectedError):
            self.pots.delete()

    def test_a_part_has_exactly_one_group(self):
        """Type is singular, which is what makes 'capacitors below minimum'
        and 'which component page owns this' unambiguous."""
        self.assertEqual(self.part.group, self.pots)
        self.assertFalse(hasattr(self.part, "groups"))


class TagTests(TestCase):
    """Cross-cutting facts, which are plural where type is singular."""

    def setUp(self):
        self.pots = Group.objects.create(name="Potentiometers", slug="potentiometers")
        self.touch = Tag.objects.create(name="touch", slug="touch")
        self.lending = Tag.objects.create(name="lending", slug="lending")

    def test_a_soft_pot_is_a_pot_that_is_also_touch(self):
        """Part 0386 is a potentiometer that is tagged touch, because touch is
        what it is for. The group says what it is; the tag says what it is for."""
        part = Part.objects.create(
            part_number="0386", short_name="linear soft potentiometer", group=self.pots
        )
        part.tags.add(self.touch)
        self.assertEqual(part.group, self.pots)
        self.assertEqual([t.slug for t in part.tags.all()], ["touch"])

    def test_a_part_can_carry_several_tags(self):
        part = Part.objects.create(part_number="0001", short_name="thing")
        part.tags.set([self.touch, self.lending])
        self.assertEqual(part.tags.count(), 2)

    def test_tags_are_optional(self):
        part = Part.objects.create(part_number="0002", short_name="thing")
        self.assertEqual(part.tags.count(), 0)


class GroupApiTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        pots = Group.objects.create(name="Potentiometers", slug="potentiometers")
        caps = Group.objects.create(name="Capacitors", slug="capacitors")
        touch = Tag.objects.create(name="touch", slug="touch")

        Part.objects.create(part_number="0390", short_name="pot", group=pots)
        soft = Part.objects.create(
            part_number="0386", short_name="soft pot", group=pots
        )
        soft.tags.add(touch)
        Part.objects.create(part_number="0054", short_name="cap", group=caps)

        _, token = ApiKey.generate("web", scope=ApiKey.Scope.READ)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")

    def test_part_serializes_its_group_and_tags(self):
        response = self.client.get("/api/v1/parts/0386/")
        self.assertEqual(response.data["group"]["slug"], "potentiometers")
        self.assertEqual(response.data["tags"], ["touch"])

    def test_filtering_by_group(self):
        """This is what ioref-web builds a component page's part list from."""
        response = self.client.get("/api/v1/parts/?group=potentiometers")
        self.assertEqual(
            sorted(p["part_number"] for p in response.data["results"]), ["0386", "0390"]
        )

    def test_filtering_by_tag(self):
        response = self.client.get("/api/v1/parts/?tag=touch")
        self.assertEqual([p["part_number"] for p in response.data["results"]], ["0386"])

    def test_groups_are_listable_with_counts(self):
        response = self.client.get("/api/v1/groups/")
        self.assertEqual(response.status_code, 200)
        counts = {g["slug"]: g["part_count"] for g in response.data["results"]}
        # part_count saves ioref-web a request per group.
        self.assertEqual(counts, {"potentiometers": 2, "capacitors": 1})

    def test_tags_are_listable(self):
        self.assertEqual(self.client.get("/api/v1/tags/").data["count"], 1)


class PartCountLinkTests(TestCase):
    """The parts count in the group, tag and location lists is a way in.

    Counting parts is only half of it: the number answers "how many" and the
    link answers "which ones", which is the question someone browsing a group
    listing actually has next.
    """

    def setUp(self):
        self.client.force_login(
            get_user_model().objects.create_superuser(
                username="admin@andrew.cmu.edu", password="x"
            )
        )
        self.group = Group.objects.create(name="Potentiometers", slug="potentiometers")
        self.empty = Group.objects.create(name="Nothing here", slug="nothing-here")
        self.tag = Tag.objects.create(name="soldering", slug="soldering")
        self.location = Location.objects.create(code="B1-R1-C1")
        for number in ("0390", "0386"):
            part = Part.objects.create(
                part_number=number,
                short_name=f"pot {number}",
                group=self.group,
                location=self.location,
            )
            part.tags.add(self.tag)

    def test_group_count_links_to_the_filtered_part_list(self):
        response = self.client.get("/admin/inventory/group/")
        # The whole anchor, not just the href: format_html output that got
        # escaped would still contain the URL as text and read as passing.
        self.assertContains(
            response,
            f'<a href="/admin/inventory/part/?group__id__exact={self.group.pk}">2</a>',
            html=True,
        )

    def test_tag_and_location_counts_link_too(self):
        self.assertContains(
            self.client.get("/admin/inventory/tag/"),
            f"/admin/inventory/part/?tags__id__exact={self.tag.pk}",
        )
        self.assertContains(
            self.client.get("/admin/inventory/location/"),
            f"/admin/inventory/part/?location__id__exact={self.location.pk}",
        )

    def test_the_link_actually_filters(self):
        # The changelist rejects a lookup that is not in list_filter, so this
        # asserts the pairing rather than just the href text.
        response = self.client.get(
            f"/admin/inventory/part/?group__id__exact={self.group.pk}"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["cl"].result_count, 2)

    def test_an_empty_group_is_not_a_link(self):
        response = self.client.get("/admin/inventory/group/")
        self.assertNotContains(
            response, f"/admin/inventory/part/?group__id__exact={self.empty.pk}"
        )

    def test_counting_does_not_cost_a_query_per_row(self):
        for n in range(12):
            Group.objects.create(name=f"Group {n}", slug=f"group-{n}")
        # Session, user, the category filter's choices, the changelist's two
        # counts, and one for the page of groups with their part counts
        # annotated on. The number matters less than the fact that it does not
        # grow with the number of rows: counting per row would make this 6 + 14.
        with self.assertNumQueries(6):
            self.client.get("/admin/inventory/group/")


class PartDeletionTests(TestCase):
    """Parts are not deletable, because their history cascades with them."""

    def setUp(self):
        self.user = get_user_model().objects.create_superuser(
            username="admin@andrew.cmu.edu", password="x"
        )
        self.client.force_login(self.user)
        self.part = Part.objects.create(part_number="0010", short_name="resistor")

    def test_the_change_page_offers_no_delete(self):
        response = self.client.get(
            reverse("admin:inventory_part_change", args=[self.part.pk])
        )
        self.assertNotContains(
            response, reverse("admin:inventory_part_delete", args=[self.part.pk])
        )

    def test_deleting_directly_is_refused(self):
        """Not merely hidden. A superuser posting the URL is still refused."""
        response = self.client.post(
            reverse("admin:inventory_part_delete", args=[self.part.pk]),
            {"post": "yes"},
        )
        self.assertEqual(response.status_code, 403)
        self.assertTrue(Part.objects.filter(pk=self.part.pk).exists())

    def test_the_bulk_action_is_withdrawn(self):
        """The more dangerous of the two: it takes no per-object confirmation."""
        request = RequestFactory().get("/")
        request.user = self.user
        actions = site._registry[Part].get_actions(request)
        self.assertNotIn("delete_selected", actions)


class RecordFromPartTests(TestCase):
    """Recording a count or a price starts and ends on the part."""

    def setUp(self):
        self.user = get_user_model().objects.create_superuser(
            username="admin@andrew.cmu.edu", password="x"
        )
        self.client.force_login(self.user)
        self.part = Part.objects.create(part_number="0010", short_name="resistor")

    def test_the_change_form_offers_both_buttons(self):
        response = self.client.get(
            reverse("admin:inventory_part_change", args=[self.part.pk])
        )
        titles = [a["title"] for a in response.context["actions_detail"]]
        self.assertEqual(titles, ["Record count", "Record price"])

    def test_the_button_opens_the_add_form_with_the_part_chosen(self):
        response = self.client.get(
            reverse("admin:inventory_part_record_count", args=[self.part.pk])
        )
        self.assertRedirects(
            response,
            f"{reverse('admin:inventory_stockevent_add')}"
            f"?part={self.part.pk}&_part={self.part.pk}",
            target_status_code=200,
        )

    def test_the_add_form_is_prefilled_with_the_logged_in_user(self):
        response = self.client.get(
            f"{reverse('admin:inventory_stockevent_add')}?part={self.part.pk}"
        )
        self.assertEqual(
            response.context["adminform"].form.initial["recorded_by"], self.user.pk
        )

    def test_saving_returns_to_the_part(self):
        """Otherwise a count recorded from a part lands on the event list."""
        add = reverse("admin:inventory_stockevent_add")
        response = self.client.post(
            f"{add}?part={self.part.pk}&_part={self.part.pk}",
            {
                "part": self.part.pk,
                "kind": StockEvent.Kind.INVENTORY,
                "quantity": 12,
                "observed_at_0": "2026-08-19",
                "observed_at_1": "10:00:00",
                "note": "",
            },
        )
        self.assertRedirects(
            response, reverse("admin:inventory_part_change", args=[self.part.pk])
        )
        self.assertEqual(self.part.stock_events.count(), 1)


class PurchaseLinkTests(TestCase):
    """Purchase links can be entered and opened from the part admin."""

    def setUp(self):
        self.part = Part.objects.create(part_number="0010", short_name="resistor")
        PriceObservation.objects.create(
            part=self.part,
            price="1.25",
            supplier="Digi-Key",
            purchase_link="https://example.invalid/thing",
            observed_at=timezone.now(),
        )

    def test_the_admin_inline_renders_a_new_tab_link(self):
        self.client.force_login(
            get_user_model().objects.create_superuser(
                username="admin@andrew.cmu.edu", password="x"
            )
        )
        response = self.client.get(
            reverse("admin:inventory_part_change", args=[self.part.pk])
        )
        self.assertContains(response, 'target="_blank"')
        self.assertContains(response, 'rel="noopener noreferrer"')

    def test_the_admin_inline_omits_the_redundant_object_label(self):
        self.client.force_login(
            get_user_model().objects.create_superuser(
                username="labels@andrew.cmu.edu", password="x"
            )
        )

        response = self.client.get(
            reverse("admin:inventory_part_change", args=[self.part.pk])
        )

        self.assertNotContains(response, str(self.part.price_observations.get()))

    def test_the_admin_inline_includes_an_editable_purchase_link(self):
        self.client.force_login(
            get_user_model().objects.create_superuser(
                username="editor@andrew.cmu.edu", password="x"
            )
        )

        response = self.client.get(
            reverse("admin:inventory_part_change", args=[self.part.pk])
        )

        self.assertContains(
            response, 'name="price_observations-__prefix__-purchase_link"'
        )

    def test_a_new_price_observation_defaults_to_now(self):
        before = timezone.now()

        observation = PriceObservation(
            part=self.part,
            price="1.50",
        )

        self.assertGreaterEqual(observation.observed_at, before)
        self.assertLessEqual(observation.observed_at, timezone.now())

    def test_the_public_page_does_too(self):
        self.client.force_login(
            get_user_model().objects.create_user(
                username="staff@andrew.cmu.edu", password="x"
            )
        )
        response = self.client.get(reverse("part_detail", args=[self.part.part_number]))
        self.assertContains(response, 'target="_blank"')


class DashboardTests(TestCase):
    """The admin index answers questions instead of listing tables."""

    def setUp(self):
        self.user = get_user_model().objects.create_superuser(
            username="admin@andrew.cmu.edu", password="x"
        )
        self.client.force_login(self.user)
        now = timezone.now()

        def part(number, minimum=None, floor=None, status=Part.Status.ACTIVE):
            p = Part.objects.create(
                part_number=number,
                short_name=f"part {number}",
                min_quantity=minimum,
                status=status,
            )
            if floor is not None:
                StockEvent.objects.create(
                    part=p,
                    kind=StockEvent.Kind.INVENTORY,
                    quantity=floor,
                    observed_at=now,
                )
            return p

        self.low = part("0001", minimum=10, floor=2)
        self.empty = part("0002", minimum=5, floor=0)
        self.healthy = part("0003", minimum=1, floor=50)
        self.never = part("0004")
        # Below its minimum but on the way out, so not work anyone should do.
        self.retired = part(
            "0005", minimum=10, floor=0, status=Part.Status.DISCONTINUED
        )

    def cards(self):
        response = self.client.get(reverse("admin:index"))
        return {c["label"]: c for c in response.context["cards"]}

    def test_counts_cover_active_parts_only(self):
        cards = self.cards()
        # 0001 and 0002 are low; 0005 is low but discontinued.
        self.assertEqual(cards["Below minimum"]["value"], 2)
        self.assertEqual(cards["Out of stock"]["value"], 1)

    def test_never_counted_ignores_status(self):
        """A part nobody has ever counted is worth seeing whatever its status."""
        self.assertEqual(self.cards()["Never counted"]["value"], 1)

    def test_each_card_links_to_a_list_that_actually_filters(self):
        for label, expected in (
            ("Below minimum", {self.low.pk, self.empty.pk}),
            ("Out of stock", {self.empty.pk}),
            ("Never counted", {self.never.pk}),
        ):
            with self.subTest(label=label):
                response = self.client.get(self.cards()[label]["url"])
                self.assertEqual(response.status_code, 200)
                shown = {p.pk for p in response.context["cl"].result_list}
                self.assertEqual(shown, expected)

    def test_the_app_list_is_gone(self):
        """It duplicated the sidebar and disagreed with it about the names."""
        response = self.client.get(reverse("admin:index"))
        self.assertNotContains(response, "Authentication and Authorization")

    def test_recent_counts_skip_imported_history(self):
        """Imported events carry no recorder and would fill the list."""
        StockEvent.objects.create(
            part=self.healthy,
            kind=StockEvent.Kind.INVENTORY,
            quantity=7,
            observed_at=timezone.now(),
            recorded_by=self.user,
        )
        response = self.client.get(reverse("admin:index"))
        recent = list(response.context["recent_counts"])
        self.assertEqual(len(recent), 1)
        self.assertEqual(recent[0].recorded_by, self.user)


class CategoryTests(TestCase):
    """The macro taxonomy: set here, rendered by whoever shows the guides.

    Named away from the five real categories the 0006 migration seeds
    (input, output, power, connector, controller), so this stays a unit test
    of the mechanism rather than a test that happens to pass only because the
    migration ran first.
    """

    def setUp(self):
        self.gadgets = Category.objects.create(name="Gadgets", slug="gadgets")
        self.resistors = Group.objects.create(
            name="Resistors", slug="resistor", category=self.gadgets
        )
        self.fasteners = Group.objects.create(name="Fasteners", slug="fasteners")
        Part.objects.create(
            part_number="0001", short_name="10k resistor", group=self.resistors
        )
        Part.objects.create(
            part_number="0002", short_name="M3 bolt", group=self.fasteners
        )
        self.loose_sensor = Part.objects.create(
            part_number="0003", short_name="Loose sensor", category=self.gadgets
        )
        self.client = APIClient()
        _, token = ApiKey.generate("ioref-web", scope=ApiKey.Scope.READ)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")

    def test_a_group_may_have_no_category(self):
        """Most of the catalog is not in the curriculum, and should not be."""
        self.assertIsNone(self.fasteners.category)

    def test_groups_carry_their_category_slug(self):
        response = self.client.get("/api/v1/groups/")
        by_slug = {g["slug"]: g for g in response.data["results"]}
        self.assertEqual(by_slug["resistor"]["category"], "gadgets")
        self.assertIsNone(by_slug["fasteners"]["category"])

    def test_groups_can_be_filtered_by_category(self):
        response = self.client.get("/api/v1/groups/?category=gadgets")
        self.assertEqual([g["slug"] for g in response.data["results"]], ["resistor"])

    def test_parts_can_be_filtered_by_category(self):
        response = self.client.get("/api/v1/parts/?category=gadgets")
        numbers = [p["part_number"] for p in response.data["results"]]
        self.assertEqual(numbers, ["0001", "0003"])

    def test_part_response_exposes_its_effective_category(self):
        grouped = self.client.get("/api/v1/parts/0001/")
        ungrouped = self.client.get("/api/v1/parts/0003/")
        self.assertEqual(grouped.data["category"], "gadgets")
        self.assertEqual(ungrouped.data["category"], "gadgets")

    def test_ungrouped_category_filter_excludes_grouped_parts(self):
        response = self.client.get("/api/v1/parts/?category=gadgets&ungrouped=true")
        self.assertEqual(
            [part["part_number"] for part in response.data["results"]], ["0003"]
        )

    def test_direct_category_is_only_for_ungrouped_parts(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            Part.objects.create(
                part_number="0004",
                short_name="Ambiguous sensor",
                group=self.resistors,
                category=self.gadgets,
            )

    def test_categories_are_listed(self):
        response = self.client.get("/api/v1/categories/")
        self.assertIn("gadgets", [c["slug"] for c in response.data["results"]])

    def test_deleting_a_category_leaves_the_group(self):
        """Retiring a heading must not take the parts filed under it."""
        self.gadgets.delete()
        self.resistors.refresh_from_db()
        self.assertIsNone(self.resistors.category)
        self.assertEqual(self.resistors.parts.count(), 1)
