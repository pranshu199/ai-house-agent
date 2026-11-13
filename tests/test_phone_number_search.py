import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from fastapi import HTTPException
from fastapi.testclient import TestClient
from supabase_auth.errors import AuthApiError
from twilio.base.exceptions import TwilioException

from app import main
from app.api import auth, phone_numbers
from app.api.auth import AuthenticatedRealtor
from app.services import phone_search


USER_ID = "supabase-user-1"
REALTOR_ID = "realtor-1"


class FakeSupabaseQuery:
    def __init__(self, data):
        self.data = data
        self.filters = []
        self.selected_fields = None
        self.row_limit = None

    def select(self, fields):
        self.selected_fields = fields
        return self

    def eq(self, field, value):
        self.filters.append((field, value))
        return self

    def limit(self, value):
        self.row_limit = value
        return self

    def execute(self):
        rows = [
            row
            for row in self.data
            if all(row.get(field) == value for field, value in self.filters)
        ]
        return SimpleNamespace(data=rows[: self.row_limit])


class FakeSupabaseClient:
    def __init__(self, user_id=USER_ID, realtors=None, auth_error=None):
        if auth_error:
            get_user = Mock(side_effect=auth_error)
        else:
            get_user = Mock(
                return_value=SimpleNamespace(
                    user=SimpleNamespace(id=user_id),
                )
            )
        self.auth = SimpleNamespace(get_user=get_user)
        self.realtors = (
            realtors
            if realtors is not None
            else [{"id": REALTOR_ID, "user_id": USER_ID}]
        )
        self.queries = []

    def table(self, table_name):
        if table_name != "realtors":
            raise AssertionError(f"Unexpected table: {table_name}")
        query = FakeSupabaseQuery(self.realtors)
        self.queries.append(query)
        return query


def available_number(
    phone_number="+14165550101",
    iso_country="CA",
    voice=True,
    **attributes,
):
    return SimpleNamespace(
        phone_number=phone_number,
        iso_country=iso_country,
        friendly_name="(416) 555-0101",
        locality="Toronto",
        region="ON",
        capabilities={"voice": voice, "SMS": True, "MMS": False, "fax": False},
        **attributes,
    )


class PhoneNumberSearchTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(main.app)
        main.app.dependency_overrides.clear()
        with phone_search._SEARCH_RATE_LOCK:
            phone_search._SEARCH_REQUESTS.clear()

    def tearDown(self):
        main.app.dependency_overrides.clear()

    def authenticated_realtor(self):
        main.app.dependency_overrides[
            auth.get_authenticated_realtor
        ] = lambda: AuthenticatedRealtor(USER_ID, REALTOR_ID)

    def post_search(self, body=None, headers=None):
        return self.client.post(
            "/api/phone-numbers/search",
            json={} if body is None else body,
            headers=headers or {},
        )

    def test_missing_bearer_token_is_rejected(self):
        response = self.post_search()

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.headers["www-authenticate"], "Bearer")

    def test_invalid_and_expired_tokens_are_rejected_by_supabase_auth(self):
        for token in ("invalid-token", "expired-token"):
            with self.subTest(token=token):
                client = FakeSupabaseClient(
                    auth_error=AuthApiError("invalid access token", 401, None)
                )
                with patch.object(auth, "get_supabase_client", return_value=client):
                    response = self.post_search(
                        headers={"Authorization": f"Bearer {token}"}
                    )

                self.assertEqual(response.status_code, 401)
                client.auth.get_user.assert_called_once_with(token)

    def test_valid_token_resolves_realtor_by_verified_user_id(self):
        supabase = FakeSupabaseClient()
        local_search = Mock(return_value=[])
        rate_limit = Mock()
        with (
            patch.object(auth, "get_supabase_client", return_value=supabase),
            patch.object(phone_numbers, "enforce_search_rate_limit", rate_limit),
            patch.object(phone_numbers, "search_canadian_local_numbers", local_search),
        ):
            response = self.post_search(
                headers={"Authorization": "Bearer verified-access-token"}
            )

        self.assertEqual(response.status_code, 200)
        supabase.auth.get_user.assert_called_once_with("verified-access-token")
        query = supabase.queries[0]
        self.assertEqual(query.filters, [("user_id", USER_ID)])
        self.assertEqual(query.row_limit, 2)
        rate_limit.assert_called_once_with(REALTOR_ID)
        local_search.assert_called_once()

    def test_user_without_exactly_one_realtor_profile_is_rejected(self):
        for profiles in (
            [],
            [
                {"id": REALTOR_ID, "user_id": USER_ID},
                {"id": "another-realtor", "user_id": USER_ID},
            ],
        ):
            with self.subTest(profiles=profiles):
                supabase = FakeSupabaseClient(realtors=profiles)
                with patch.object(auth, "get_supabase_client", return_value=supabase):
                    response = self.post_search(
                        headers={"Authorization": "Bearer verified-access-token"}
                    )

                self.assertEqual(response.status_code, 403)

    def test_client_cannot_supply_realtor_id(self):
        self.authenticated_realtor()

        response = self.post_search({"realtor_id": "someone-elses-realtor"})

        self.assertEqual(response.status_code, 422)

    def test_invalid_area_code_is_rejected_before_provider_search(self):
        self.authenticated_realtor()
        with patch.object(phone_search, "get_twilio_client") as twilio_client:
            response = self.post_search({"area_code": "4x6"})

        self.assertEqual(response.status_code, 422)
        twilio_client.assert_not_called()

    def test_search_is_canadian_local_voice_only_and_filters_provider_results(self):
        self.authenticated_realtor()
        local_list = Mock(
            return_value=[
                available_number(voice=False),
                available_number(phone_number="+12125550101", iso_country="US"),
                available_number(),
            ]
        )
        twilio_client = Mock()
        twilio_client.available_phone_numbers.return_value.local.list = local_list
        with patch.object(
            phone_search,
            "get_twilio_client",
            return_value=twilio_client,
        ):
            response = self.post_search(
                {"area_code": "416", "locality": " Toronto ", "limit": 5}
            )

        self.assertEqual(response.status_code, 200)
        twilio_client.available_phone_numbers.assert_called_once_with("CA")
        local_list.assert_called_once_with(
            area_code=416,
            in_locality="Toronto",
            voice_enabled=True,
            limit=5,
        )
        self.assertEqual(response.json()["count"], 1)
        self.assertEqual(
            response.json()["numbers"][0],
            {
                "phone_number": "+14165550101",
                "friendly_name": "(416) 555-0101",
                "locality": "Toronto",
                "region": "ON",
                "capabilities": {
                    "voice": True,
                    "sms": True,
                    "mms": False,
                    "fax": False,
                },
            },
        )

    def test_twilio_failure_is_safely_reported(self):
        self.authenticated_realtor()
        local_list = Mock(side_effect=TwilioException("sensitive provider response"))
        twilio_client = Mock()
        twilio_client.available_phone_numbers.return_value.local.list = local_list
        with patch.object(
            phone_search,
            "get_twilio_client",
            return_value=twilio_client,
        ):
            response = self.post_search()

        self.assertEqual(response.status_code, 502)
        self.assertNotIn("sensitive provider response", response.text)

    def test_empty_results_return_empty_list(self):
        self.authenticated_realtor()
        twilio_client = Mock()
        twilio_client.available_phone_numbers.return_value.local.list = Mock(
            return_value=[]
        )
        with patch.object(
            phone_search,
            "get_twilio_client",
            return_value=twilio_client,
        ):
            response = self.post_search()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"numbers": [], "count": 0})

    def test_search_result_limit_is_bounded(self):
        self.authenticated_realtor()
        local_list = Mock(return_value=[])
        twilio_client = Mock()
        twilio_client.available_phone_numbers.return_value.local.list = local_list
        with patch.object(
            phone_search,
            "get_twilio_client",
            return_value=twilio_client,
        ):
            too_many = self.post_search({"limit": 21})
            maximum = self.post_search({"limit": 20})

        self.assertEqual(too_many.status_code, 422)
        self.assertEqual(maximum.status_code, 200)
        self.assertEqual(local_list.call_args.kwargs["limit"], 20)

    def test_realtor_search_rate_limit_is_enforced(self):
        for _ in range(phone_search._RATE_LIMIT_PER_WINDOW):
            phone_search.enforce_search_rate_limit(REALTOR_ID, now=1000)

        with self.assertRaises(HTTPException) as raised:
            phone_search.enforce_search_rate_limit(REALTOR_ID, now=1000)

        self.assertEqual(raised.exception.status_code, 429)


if __name__ == "__main__":
    unittest.main()
