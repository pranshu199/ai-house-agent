import json
import re
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from app.integrations import supabase_client
from app.services import lead_capture


CALL_ID = "00000000-0000-4000-8000-000000000001"
REALTOR_ID = "00000000-0000-4000-8000-000000000002"
PROPERTY_ID = "00000000-0000-4000-8000-000000000003"
CAMPAIGN_ID = "00000000-0000-4000-8000-000000000004"
LEAD_ID = "00000000-0000-4000-8000-000000000005"
CALLER_PHONE = "+14165550199"


class FakeResponse:
    def __init__(self, data):
        self.data = data


class FakeQuery:
    def __init__(self, client, table_name):
        self.client = client
        self.table_name = table_name
        self.filters = {}
        self.limit_value = None
        self.inserted_data = None
        self.updated_data = None

    def select(self, _fields):
        return self

    def eq(self, field, value):
        self.filters[field] = value
        return self

    def limit(self, value):
        self.limit_value = value
        return self

    def insert(self, data):
        self.inserted_data = dict(data)
        return self

    def update(self, data):
        self.updated_data = dict(data)
        return self

    def execute(self):
        self.client.executed_queries.append(
            (self.table_name, dict(self.filters), self.limit_value, self.inserted_data is not None)
        )
        rows = self.client.tables.setdefault(self.table_name, [])
        if self.inserted_data is not None:
            if self.table_name == "leads" and self.client.lead_insert_error is not None:
                concurrent_lead = self.client.concurrent_lead
                if concurrent_lead is not None:
                    rows.append(concurrent_lead)
                raise self.client.lead_insert_error
            row = dict(self.inserted_data)
            row["id"] = row.get("id", str(uuid.uuid4()))
            if self.table_name == "leads":
                row["normalized_phone"] = supabase_client._normalize_lead_phone(
                    row.get("phone")
                )
            rows.append(row)
            return FakeResponse([{"id": row["id"]}])

        matched = [
            row
            for row in rows
            if all(row.get(field) == value for field, value in self.filters.items())
        ]
        if self.updated_data is not None:
            for row in matched:
                row.update(self.updated_data)
            return FakeResponse([{"id": row["id"]} for row in matched])

        if self.limit_value is not None:
            matched = matched[: self.limit_value]
        return FakeResponse(matched)


class FakeSupabaseClient:
    def __init__(self, tables):
        self.tables = tables
        self.executed_queries = []
        self.lead_insert_error = None
        self.concurrent_lead = None

    def table(self, table_name):
        return FakeQuery(self, table_name)


def extraction(**overrides):
    result = {
        "name": None,
        "email": None,
        "lead_type": "unknown",
        "intent": "unknown",
        "budget_min": None,
        "budget_max": None,
        "timeline": None,
        "interested_property": None,
        "summary": "Caller asked about the property.",
        "evidence": [],
    }
    result.update(overrides)
    return result


def complete_call(**overrides):
    call = {
        "id": CALL_ID,
        "realtor_id": REALTOR_ID,
        "campaign_id": CAMPAIGN_ID,
        "property_id": PROPERTY_ID,
        "lead_id": None,
        "caller_phone": CALLER_PHONE,
        "transcript": json.dumps(
            [{"role": "USER", "text": "Could I view this property?"}]
        ),
        "ai_summary": "Caller asked to view the property.",
        "status": "completed",
    }
    call.update(overrides)
    return call


def attribution_tables(call, leads=None, property_realtor_id=REALTOR_ID):
    return {
        "calls": [call],
        "properties": [{"id": PROPERTY_ID, "realtor_id": property_realtor_id}],
        "campaigns": [
            {
                "id": CAMPAIGN_ID,
                "realtor_id": REALTOR_ID,
                "property_id": PROPERTY_ID,
            }
        ],
        "leads": leads or [],
    }


class LeadScoringTests(unittest.TestCase):
    def test_scoring_awards_only_supported_rules_and_caps_at_100(self):
        lead = extraction(
            name="Sam",
            intent="viewing_request",
            lead_type="buyer",
            timeline="within 30 days",
            interested_property="12 Main Street",
            budget_min=500000,
            budget_max=650000,
        )
        transcript = [
            {"role": "USER", "text": "Could I view it? What are the fees?"},
            {"role": "ASSISTANT", "text": "Which fees do you mean?"},
        ]
        self.assertEqual(lead_capture.score_lead(lead, transcript), (75, "hot"))

    def test_timeline_scoring_requires_explicit_30_day_window(self):
        for timeline, expected in (
            ("within 30 days", ("buying", 25)),
            ("in 4 weeks", ("selling", 25)),
            ("within 31 days", ("buying", 0)),
            ("sometime soon", ("buying", 0)),
            ("this month", ("buying", 0)),
            ("within 1 month", ("buying", 0)),
            ("within 30 days", ("unknown", 0)),
            ("within 30 days", ("property_inquiry", 0)),
        ):
            with self.subTest(timeline=timeline):
                lead = extraction(
                    lead_type="buyer",
                    intent=expected[0],
                    timeline=timeline,
                )
                self.assertEqual(
                    lead_capture.score_lead(lead, [])[0],
                    expected[1],
                )

    def test_priority_boundaries(self):
        for score, expected in (
            (0, "cold"),
            (39, "cold"),
            (40, "warm"),
            (69, "warm"),
            (70, "hot"),
            (100, "hot"),
        ):
            with self.subTest(score=score):
                self.assertEqual(lead_capture.priority_for_score(score), expected)

    def test_assistant_questions_do_not_contribute_to_score(self):
        lead = extraction()
        transcript = [
            {"role": "ASSISTANT", "text": "What is your budget? What do you need?"},
            {"role": "USER", "text": "I am just looking."},
        ]
        self.assertEqual(lead_capture.score_lead(lead, transcript), (0, "cold"))


class LeadPersistenceTests(unittest.TestCase):
    def test_python_phone_normalization_matches_e164_formatting_rules(self):
        for value, expected in (
            (CALLER_PHONE, CALLER_PHONE),
            ("+1 (416) 555-0199", CALLER_PHONE),
            ("+1-416-555-0199", CALLER_PHONE),
            ("+1.(416).555.0199", CALLER_PHONE),
            ("+1 (416)-555.0199", CALLER_PHONE),
            ("  +14165550199", None),
            ("+14165550199 ", CALLER_PHONE),
            ("\t+14165550199", None),
            ("+14165 550199", "+14165550199"),
            ("+14165\t550199", None),
            ("+14165\n550199", None),
            ("+1/416/555/0199", None),
            ("4165550199", None),
            ("+01234567", None),
            ("+1234567", None),
            ("+1234567890123456", None),
        ):
            with self.subTest(value=value):
                self.assertEqual(
                    supabase_client._normalize_lead_phone(value),
                    expected,
                )

    def test_python_phone_normalization_matches_migration_expression(self):
        migration_path = (
            Path(__file__).parents[1]
            / "migrations"
            / "20261115220000_lead_intent_capture.sql"
        )
        migration = migration_path.read_text()
        allowed_phone_pattern = r"\A\+[0-9 ().-]+\Z"
        normalized_e164_pattern = r"\A\+[1-9][0-9]{7,14}\Z"
        replacement_expression = "regexp_replace(phone, '[^0-9]', '', 'g')"

        self.assertEqual(
            migration.count(f"phone ~ '{allowed_phone_pattern}'"),
            2,
        )
        self.assertEqual(
            migration.count(f"~ '{normalized_e164_pattern}'"),
            2,
        )
        self.assertEqual(migration.count(replacement_expression), 4)

        def normalize_using_migration_expression(value):
            if re.fullmatch(r"\+[0-9 ().-]+", value) is None:
                return None
            normalized = "+" + re.sub(r"[^0-9]", "", value)
            if re.fullmatch(r"\+[1-9][0-9]{7,14}", normalized) is None:
                return None
            return normalized

        cases = (
            CALLER_PHONE,
            "+1 (416) 555-0199",
            "+1-416-555-0199",
            "+1.(416).555.0199",
            " +14165550199",
            "+14165550199 ",
            "+14165 550199",
            "+14165\t550199",
            "+1/416/555/0199",
            "+01234567",
            "+1234567",
            "+1234567890123456",
        )
        for value in cases:
            with self.subTest(value=value):
                self.assertEqual(
                    supabase_client._normalize_lead_phone(value),
                    normalize_using_migration_expression(value),
                )

    def test_normalized_matching_is_database_filtered_and_pagination_safe(self):
        matching = {
            "id": LEAD_ID,
            "realtor_id": REALTOR_ID,
            "phone": "+1 (416) 555-0199",
            "normalized_phone": CALLER_PHONE,
        }
        unrelated = [
            {
                "id": str(uuid.uuid4()),
                "realtor_id": REALTOR_ID,
                "phone": f"+1416556{index:04d}",
                "normalized_phone": f"+1416556{index:04d}",
            }
            for index in range(250)
        ]
        call = complete_call()
        client = FakeSupabaseClient(
            attribution_tables(call, leads=[*unrelated, matching])
        )
        with patch.object(supabase_client, "get_supabase_client", return_value=client):
            lead_id = supabase_client.persist_call_lead(
                CALL_ID,
                extraction(),
                40,
                "warm",
            )

        self.assertEqual(lead_id, LEAD_ID)
        lead_queries = [
            (filters, limit)
            for table, filters, limit, is_insert in client.executed_queries
            if not is_insert
            if table == "leads" and "normalized_phone" in filters
        ]
        self.assertEqual(
            lead_queries,
            [({"realtor_id": REALTOR_ID, "normalized_phone": CALLER_PHONE}, 2)],
        )
        self.assertEqual(client.tables["calls"][0]["lead_id"], LEAD_ID)

    def test_existing_lead_is_matched_updated_and_linked_without_null_overwrites(self):
        existing_lead = {
            "id": LEAD_ID,
            "realtor_id": REALTOR_ID,
            "phone": "+1 (416) 555-0199",
            "normalized_phone": CALLER_PHONE,
            "name": "Sam Caller",
            "email": "sam@example.test",
            "budget_min": 600000,
            "budget_max": 700000,
            "timeline": "within 2 months",
            "lead_type": "buyer",
            "intent": "buying",
            "summary": "Caller disclosed a two month move plan.",
            "intent_score": 70,
            "priority": "hot",
        }
        client = FakeSupabaseClient(
            attribution_tables(complete_call(), leads=[existing_lead])
        )
        with patch.object(supabase_client, "get_supabase_client", return_value=client):
            lead_id = supabase_client.persist_call_lead(
                CALL_ID,
                extraction(
                    name="",
                    email=None,
                    lead_type="unknown",
                    intent="unknown",
                    budget_min=100000,
                    budget_max=200000,
                    timeline="soon",
                    summary="Caller asked about the property.",
                ),
                40,
                "warm",
            )

        self.assertEqual(lead_id, LEAD_ID)
        self.assertEqual(existing_lead["name"], "Sam Caller")
        self.assertEqual(existing_lead["email"], "sam@example.test")
        self.assertEqual(existing_lead["budget_min"], 600000)
        self.assertEqual(existing_lead["budget_max"], 700000)
        self.assertEqual(existing_lead["timeline"], "within 2 months")
        self.assertEqual(existing_lead["summary"], "Caller disclosed a two month move plan.")
        self.assertEqual(existing_lead["intent"], "buying")
        self.assertEqual(existing_lead["intent_score"], 70)
        self.assertEqual(existing_lead["priority"], "hot")
        self.assertEqual(client.tables["calls"][0]["lead_id"], LEAD_ID)

    def test_new_lead_receives_call_attribution_and_is_linked(self):
        client = FakeSupabaseClient(attribution_tables(complete_call()))
        with patch.object(supabase_client, "get_supabase_client", return_value=client):
            lead_id = supabase_client.persist_call_lead(
                CALL_ID,
                extraction(
                    name="Sam",
                    lead_type="buyer",
                    intent="viewing_request",
                    interested_property="12 Main Street",
                ),
                45,
                "warm",
            )

        created = client.tables["leads"][0]
        self.assertEqual(lead_id, created["id"])
        self.assertEqual(created["realtor_id"], REALTOR_ID)
        self.assertEqual(created["source_campaign_id"], CAMPAIGN_ID)
        self.assertEqual(created["original_property_id"], PROPERTY_ID)
        self.assertEqual(created["phone"], CALLER_PHONE)
        self.assertEqual(created["intent_score"], 45)
        self.assertEqual(created["priority"], "warm")
        self.assertEqual(client.tables["calls"][0]["lead_id"], lead_id)

    def test_only_unique_constraint_error_recovers_after_concurrent_insert(self):
        concurrent_lead = {
            "id": LEAD_ID,
            "realtor_id": REALTOR_ID,
            "phone": CALLER_PHONE,
            "normalized_phone": CALLER_PHONE,
            "name": "Existing from another call",
            "intent_score": 10,
        }
        client = FakeSupabaseClient(attribution_tables(complete_call()))
        error = RuntimeError("duplicate key")
        error.code = "23505"
        client.lead_insert_error = error
        client.concurrent_lead = concurrent_lead
        with patch.object(supabase_client, "get_supabase_client", return_value=client):
            lead_id = supabase_client.persist_call_lead(
                CALL_ID,
                extraction(name="Sam"),
                40,
                "warm",
            )

        self.assertEqual(lead_id, LEAD_ID)
        self.assertEqual(concurrent_lead["name"], "Existing from another call")
        self.assertEqual(concurrent_lead["intent_score"], 40)
        self.assertEqual(client.tables["calls"][0]["lead_id"], LEAD_ID)

    def test_non_unique_database_error_is_propagated_without_relookup(self):
        client = FakeSupabaseClient(attribution_tables(complete_call()))
        error = RuntimeError("database unavailable")
        error.code = "08006"
        client.lead_insert_error = error
        with patch.object(supabase_client, "get_supabase_client", return_value=client):
            with self.assertRaisesRegex(RuntimeError, "database unavailable"):
                supabase_client.persist_call_lead(CALL_ID, extraction(), 0, "cold")

        self.assertEqual(
            sum(
                table == "leads" and "normalized_phone" in filters
                for table, filters, _, _ in client.executed_queries
            ),
            1,
        )

    def test_invalid_property_relationship_prevents_lead_persistence(self):
        client = FakeSupabaseClient(
            attribution_tables(complete_call(), property_realtor_id=CAMPAIGN_ID)
        )
        with patch.object(supabase_client, "get_supabase_client", return_value=client):
            with self.assertRaisesRegex(ValueError, "property does not belong"):
                supabase_client.persist_call_lead(CALL_ID, extraction(), 0, "cold")

        self.assertEqual(client.tables["leads"], [])
        self.assertIsNone(client.tables["calls"][0]["lead_id"])

    def test_incomplete_call_cannot_create_a_lead(self):
        client = FakeSupabaseClient(
            attribution_tables(complete_call(status="failed"))
        )
        with patch.object(supabase_client, "get_supabase_client", return_value=client):
            with self.assertRaisesRegex(ValueError, "completed call"):
                supabase_client.persist_call_lead(CALL_ID, extraction(), 0, "cold")
        self.assertEqual(client.tables["leads"], [])
