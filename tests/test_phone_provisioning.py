import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient
from postgrest.exceptions import APIError
from requests.exceptions import Timeout
from twilio.base.exceptions import TwilioException

from app import main as app_module
from app.api import auth, phone_numbers
from app.integrations import twilio_client
from app.services.phone_provisioning import (
    ActivePhoneNumberExists,
    IdempotencyConflict,
    InvalidProvisioningRequest,
    InvalidLifecycleTransition,
    PhoneProvisioningService,
    PropertyOwnershipError,
    ProvisioningDatabaseError,
    ProvisioningInProgress,
    ProvisioningStatus,
)
from app.schemas.auth import AuthenticatedRealtor


USER_ID = "user-1"
REALTOR_ID = "realtor-1"
PROPERTY_ID = "property-1"
PRINCIPAL = AuthenticatedRealtor(USER_ID, REALTOR_ID)


class FakeResponse:
    def __init__(self, data):
        self.data = data


class FakeQuery:
    def __init__(self, client, table_name):
        self.client = client
        self.table_name = table_name
        self.filters = {}
        self.in_filters = {}
        self.row_limit = None
        self.inserted_data = None
        self.updated_data = None

    def select(self, fields):
        return self

    def eq(self, field, value):
        self.filters[field] = value
        return self

    def in_(self, field, values):
        self.in_filters[field] = values
        return self

    def limit(self, value):
        self.row_limit = value
        return self

    def insert(self, data):
        self.inserted_data = data
        return self

    def update(self, data):
        self.updated_data = data
        return self

    def execute(self):
        failure_key = (self.table_name, "insert" if self.inserted_data else "execute")
        if self.client.fail_next == failure_key:
            self.client.fail_next = None
            raise APIError(
                {"message": "database unavailable", "code": "XX000"}
            )

        rows = self.client.tables.setdefault(self.table_name, [])
        if self.inserted_data is not None:
            row = {
                **self.inserted_data,
                "id": f"request-{len(rows) + 1}",
                "phone_number": None,
                "twilio_sid": None,
                "last_error_code": None,
                "completed_at": None,
            }
            rows.append(row)
            return FakeResponse([dict(row)])

        matching_rows = [
            row
            for row in rows
            if all(row.get(field) == value for field, value in self.filters.items())
            and all(
                row.get(field) in values
                for field, values in self.in_filters.items()
            )
        ]
        if self.row_limit is not None:
            matching_rows = matching_rows[: self.row_limit]

        if self.updated_data is not None:
            for row in matching_rows:
                row.update(self.updated_data)

        return FakeResponse([dict(row) for row in matching_rows])


class FakeRPC:
    def __init__(self, client, name, parameters):
        self.client = client
        self.name = name
        self.parameters = parameters

    def execute(self):
        self.client.rpc_calls.append((self.name, self.parameters))
        if self.client.fail_rpc:
            self.client.fail_rpc = False
            raise APIError({"message": "database unavailable", "code": "XX000"})
        return FakeResponse(
            [
                {
                    "provisioning_request_id": self.parameters["p_request_id"],
                    "assigned_phone_number_id": "phone-assignment-1",
                }
            ]
        )


class FakeSupabaseClient:
    def __init__(self, *, property_realtor_id=REALTOR_ID, phone_numbers=None):
        self.tables = {
            "properties": [
                {"id": PROPERTY_ID, "realtor_id": property_realtor_id},
            ],
            "phone_numbers": phone_numbers or [],
            "phone_number_provisioning": [],
        }
        self.fail_next = None
        self.fail_rpc = False
        self.rpc_calls = []

    def table(self, table_name):
        return FakeQuery(self, table_name)

    def rpc(self, name, parameters):
        return FakeRPC(self, name, parameters)


class PhoneProvisioningServiceTests(unittest.TestCase):
    def setUp(self):
        self.client = FakeSupabaseClient()
        self.service = PhoneProvisioningService(self.client)

    def create_request(self, **overrides):
        values = {
            "principal": PRINCIPAL,
            "property_id": PROPERTY_ID,
            "idempotency_key": "idempotency-key-1",
            "requested_area_code": "416",
            "requested_locality": "Toronto",
        }
        values.update(overrides)
        return self.service.create_request(**values)

    def test_create_request_is_idempotent_for_matching_input(self):
        first = self.create_request()
        second = self.create_request()

        self.assertEqual(first["id"], second["id"])
        self.assertEqual(len(self.client.tables["phone_number_provisioning"]), 1)

    def test_idempotency_key_cannot_be_reused_with_different_input(self):
        self.create_request()

        with self.assertRaises(IdempotencyConflict):
            self.create_request(requested_locality="Ottawa")

    def test_property_must_belong_to_authenticated_realtor(self):
        client = FakeSupabaseClient(property_realtor_id="another-realtor")
        service = PhoneProvisioningService(client)

        with self.assertRaises(PropertyOwnershipError):
            service.create_request(
                principal=PRINCIPAL,
                property_id=PROPERTY_ID,
                idempotency_key="idempotency-key-1",
            )

        self.assertEqual(client.tables["phone_number_provisioning"], [])

    def test_existing_active_number_prevents_a_new_request(self):
        client = FakeSupabaseClient(
            phone_numbers=[
                {
                    "id": "demo-phone",
                    "property_id": PROPERTY_ID,
                    "status": "active",
                }
            ]
        )
        service = PhoneProvisioningService(client)

        with self.assertRaises(ActivePhoneNumberExists):
            service.create_request(
                principal=PRINCIPAL,
                property_id=PROPERTY_ID,
                idempotency_key="idempotency-key-1",
            )

        self.assertEqual(client.tables["phone_numbers"][0]["id"], "demo-phone")
        self.assertEqual(client.tables["phone_number_provisioning"], [])

    def test_unresolved_request_prevents_duplicate_in_flight_work(self):
        self.create_request()

        with self.assertRaises(ProvisioningInProgress):
            self.create_request(idempotency_key="idempotency-key-2")

        self.assertEqual(len(self.client.tables["phone_number_provisioning"]), 1)

    def test_invalid_lifecycle_transition_is_rejected(self):
        request = self.create_request()

        with self.assertRaises(InvalidLifecycleTransition):
            self.service.transition_request(
                request_id=request["id"],
                principal=PRINCIPAL,
                target_status=ProvisioningStatus.COMPLETED,
            )

        self.assertEqual(
            self.client.tables["phone_number_provisioning"][0]["status"],
            ProvisioningStatus.PENDING.value,
        )

    def test_failed_attempt_can_recover_and_retries_increment_attempts(self):
        request = self.create_request()
        request_id = request["id"]

        self.service.transition_request(
            request_id=request_id,
            principal=PRINCIPAL,
            target_status=ProvisioningStatus.PURCHASE_IN_PROGRESS,
        )
        self.service.transition_request(
            request_id=request_id,
            principal=PRINCIPAL,
            target_status=ProvisioningStatus.PURCHASE_FAILED,
            last_error_code="provider_timeout",
        )
        recovered = self.service.transition_request(
            request_id=request_id,
            principal=PRINCIPAL,
            target_status=ProvisioningStatus.PURCHASE_IN_PROGRESS,
        )

        self.assertEqual(recovered["attempts"], 2)
        self.assertIsNone(recovered["last_error_code"])

    def test_purchase_start_persists_selected_number_before_provider_call(self):
        request = self.create_request()

        started = self.service.start_purchase(
            principal=PRINCIPAL,
            request_id=request["id"],
            phone_number="+14165550101",
        )

        self.assertEqual(started["status"], "purchase_in_progress")
        self.assertEqual(started["phone_number"], "+14165550101")
        self.assertEqual(started["attempts"], 1)

        with self.assertRaises(InvalidLifecycleTransition):
            self.service.start_purchase(
                principal=PRINCIPAL,
                request_id=request["id"],
                phone_number="+14165550102",
            )

        self.assertEqual(
            self.client.tables["phone_number_provisioning"][0]["phone_number"],
            "+14165550101",
        )

    def test_purchase_result_cannot_change_persisted_number(self):
        request = self.create_request()
        self.service.start_purchase(
            principal=PRINCIPAL,
            request_id=request["id"],
            phone_number="+14165550101",
        )

        with self.assertRaises(InvalidLifecycleTransition):
            self.service.record_purchase_result(
                principal=PRINCIPAL,
                request_id=request["id"],
                country_code="CA",
                phone_number="+14165550102",
                twilio_sid=f"PN{'a' * 32}",
            )

    def test_database_failure_does_not_change_state_and_can_be_retried(self):
        request = self.create_request()
        self.client.fail_next = ("phone_number_provisioning", "execute")

        with self.assertRaises(ProvisioningDatabaseError):
            self.service.transition_request(
                request_id=request["id"],
                principal=PRINCIPAL,
                target_status=ProvisioningStatus.PURCHASE_IN_PROGRESS,
            )

        self.assertEqual(
            self.client.tables["phone_number_provisioning"][0]["status"],
            ProvisioningStatus.PENDING.value,
        )
        retried = self.service.transition_request(
            request_id=request["id"],
            principal=PRINCIPAL,
            target_status=ProvisioningStatus.PURCHASE_IN_PROGRESS,
        )
        self.assertEqual(retried["status"], ProvisioningStatus.PURCHASE_IN_PROGRESS)

    def test_assignment_uses_database_rpc_after_purchase_and_configuration_states(self):
        request = self.create_request()
        request_id = request["id"]
        self.service.transition_request(
            request_id=request_id,
            principal=PRINCIPAL,
            target_status=ProvisioningStatus.PURCHASE_IN_PROGRESS,
        )
        self.service.record_purchase_result(
            request_id=request_id,
            principal=PRINCIPAL,
            country_code="CA",
            phone_number="+14165550101",
            twilio_sid=f"PN{'a' * 32}",
        )
        self.service.transition_request(
            request_id=request_id,
            principal=PRINCIPAL,
            target_status=ProvisioningStatus.CONFIGURATION_IN_PROGRESS,
        )

        assignment = self.service.complete_assignment(
            request_id=request_id,
            principal=PRINCIPAL,
        )

        self.assertEqual(
            assignment["assigned_phone_number_id"],
            "phone-assignment-1",
        )
        self.assertEqual(
            self.client.rpc_calls,
            [
                (
                    "complete_phone_number_provisioning",
                    {
                        "p_request_id": request_id,
                        "p_realtor_id": REALTOR_ID,
                    },
                )
            ],
        )

    def test_purchase_result_rejects_non_canadian_provider_region(self):
        request = self.create_request()
        self.service.transition_request(
            request_id=request["id"],
            principal=PRINCIPAL,
            target_status=ProvisioningStatus.PURCHASE_IN_PROGRESS,
        )

        with self.assertRaises(InvalidProvisioningRequest):
            self.service.record_purchase_result(
                request_id=request["id"],
                principal=PRINCIPAL,
                country_code="US",
                phone_number="+14165550101",
                twilio_sid=f"PN{'a' * 32}",
            )

        self.assertEqual(
            self.client.tables["phone_number_provisioning"][0]["status"],
            ProvisioningStatus.PURCHASE_IN_PROGRESS.value,
        )


class TwilioPhoneNumberAdapterTests(unittest.TestCase):
    def setUp(self):
        self.twilio = Mock()
        self.twilio.available_phone_numbers.return_value.local.list.return_value = [
            SimpleNamespace(
                phone_number="+14165550101",
                iso_country="CA",
                capabilities={"voice": True},
            )
        ]
        self.twilio.incoming_phone_numbers.create.return_value = SimpleNamespace(
            phone_number="+14165550101",
            iso_country="CA",
            sid=f"PN{'a' * 32}",
        )

    def test_purchase_revalidates_voice_inventory_and_configures_voice_webhook(self):
        with (
            patch.object(twilio_client, "get_twilio_client", return_value=self.twilio),
            patch.dict("os.environ", {"PUBLIC_BASE_URL": "https://voice.example.test"}),
        ):
            result = twilio_client.purchase_canadian_local_number(
                "+14165550101",
                "request-1",
            )

        self.assertEqual(result.phone_number, "+14165550101")
        self.twilio.available_phone_numbers.assert_called_once_with("CA")
        self.twilio.available_phone_numbers.return_value.local.list.assert_called_once_with(
            area_code=416,
            contains="5550101",
            voice_enabled=True,
            limit=20,
        )
        self.twilio.incoming_phone_numbers.create.assert_called_once_with(
            phone_number="+14165550101",
            friendly_name="AIHA provisioning request-1",
            voice_url="https://voice.example.test/twilio/voice",
            voice_method="POST",
        )
        self.assertNotIn(
            "status_callback",
            self.twilio.incoming_phone_numbers.create.call_args.kwargs,
        )

    def test_purchase_attaches_configured_canadian_regulatory_bundle(self):
        with (
            patch.object(twilio_client, "get_twilio_client", return_value=self.twilio),
            patch.dict(
                "os.environ",
                {
                    "PUBLIC_BASE_URL": "https://voice.example.test",
                    "TWILIO_CA_REGULATORY_BUNDLE_SID": f"BU{'a' * 32}",
                },
            ),
        ):
            twilio_client.purchase_canadian_local_number(
                "+14165550101",
                "request-1",
            )

        self.assertEqual(
            self.twilio.incoming_phone_numbers.create.call_args.kwargs["bundle_sid"],
            f"BU{'a' * 32}",
        )

    def test_non_voice_inventory_is_never_purchased(self):
        self.twilio.available_phone_numbers.return_value.local.list.return_value[0].capabilities = {
            "voice": False
        }
        with patch.object(twilio_client, "get_twilio_client", return_value=self.twilio):
            with self.assertRaises(ValueError):
                twilio_client.purchase_canadian_local_number(
                    "+14165550101",
                    "request-1",
                )

        self.twilio.incoming_phone_numbers.create.assert_not_called()

    def test_inventory_lookup_failure_is_distinguished_from_purchase_failure(self):
        self.twilio.available_phone_numbers.return_value.local.list.side_effect = (
            TwilioException("inventory unavailable")
        )
        with patch.object(twilio_client, "get_twilio_client", return_value=self.twilio):
            with self.assertRaises(twilio_client.TwilioInventoryLookupError):
                twilio_client.purchase_canadian_local_number(
                    "+14165550101",
                    "request-1",
                )

        self.twilio.incoming_phone_numbers.create.assert_not_called()

    def test_reconciliation_requires_request_marker_and_exact_number(self):
        self.twilio.incoming_phone_numbers.list.return_value = [
            SimpleNamespace(
                phone_number="+14165550101",
                iso_country="CA",
                friendly_name="AIHA provisioning request-1",
            )
        ]
        with patch.object(twilio_client, "get_twilio_client", return_value=self.twilio):
            found = twilio_client.find_provisioned_canadian_number(
                "+14165550101",
                "request-1",
            )

        self.assertIsNotNone(found)
        self.twilio.incoming_phone_numbers.list.assert_called_once_with(
            phone_number="+14165550101",
            friendly_name="AIHA provisioning request-1",
            limit=20,
        )

    def test_voice_configuration_updates_only_the_existing_webhook(self):
        with (
            patch.object(twilio_client, "get_twilio_client", return_value=self.twilio),
            patch.dict("os.environ", {"PUBLIC_BASE_URL": "https://voice.example.test"}),
        ):
            twilio_client.configure_twilio_voice(f"PN{'e' * 32}")

        self.twilio.incoming_phone_numbers.assert_called_once_with(f"PN{'e' * 32}")
        self.twilio.incoming_phone_numbers.return_value.update.assert_called_once_with(
            voice_url="https://voice.example.test/twilio/voice",
            voice_method="POST",
        )

    def test_release_deletes_only_the_requested_twilio_sid(self):
        self.twilio.incoming_phone_numbers.return_value.delete.return_value = True
        with patch.object(twilio_client, "get_twilio_client", return_value=self.twilio):
            result = twilio_client.release_twilio_number(f"PN{'f' * 32}")

        self.assertTrue(result)
        self.twilio.incoming_phone_numbers.assert_called_once_with(f"PN{'f' * 32}")
        self.twilio.incoming_phone_numbers.return_value.delete.assert_called_once_with()


class PhoneNumberProvisioningAPITests(unittest.TestCase):
    def setUp(self):
        self.http = TestClient(app_module.app)
        app_module.app.dependency_overrides.clear()
        app_module.app.dependency_overrides[
            auth.get_authenticated_realtor
        ] = lambda: PRINCIPAL
        self.database = FakeSupabaseClient()
        self.service = PhoneProvisioningService(self.database)
        self.service_patch = patch.object(
            phone_numbers,
            "PhoneProvisioningService",
            return_value=self.service,
        )
        self.service_patch.start()

    def tearDown(self):
        self.service_patch.stop()
        app_module.app.dependency_overrides.clear()

    def create_request(self):
        response = self.http.post(
            "/api/phone-numbers/provisioning",
            json={
                "property_id": PROPERTY_ID,
                "idempotency_key": "api-key-1",
                "area_code": "416",
                "locality": "Toronto",
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["request"]

    def test_purchase_configures_and_assigns_through_completion_rpc(self):
        request = self.create_request()
        purchased_number = SimpleNamespace(
            phone_number="+14165550101",
            iso_country="CA",
            sid=f"PN{'a' * 32}",
        )
        with (
            patch.object(
                phone_numbers.twilio_client,
                "purchase_canadian_local_number",
                return_value=purchased_number,
            ) as purchase,
            patch.object(
                phone_numbers.twilio_client,
                "configure_twilio_voice",
            ) as configure,
        ):
            response = self.http.post(
                f"/api/phone-numbers/provisioning/{request['id']}/purchase",
                json={"phone_number": "+14165550101"},
            )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], "completed")
        purchase.assert_called_once_with("+14165550101", request["id"])
        configure.assert_called_once_with(purchased_number.sid)
        self.assertEqual(
            self.database.rpc_calls[0][0],
            "complete_phone_number_provisioning",
        )

    def test_configuration_failure_retries_without_a_second_purchase(self):
        request = self.create_request()
        purchased_number = SimpleNamespace(
            phone_number="+14165550101",
            iso_country="CA",
            sid=f"PN{'a' * 32}",
        )
        with (
            patch.object(
                phone_numbers.twilio_client,
                "purchase_canadian_local_number",
                return_value=purchased_number,
            ) as purchase,
            patch.object(
                phone_numbers.twilio_client,
                "configure_twilio_voice",
                side_effect=[TwilioException("unavailable"), None],
            ) as configure,
        ):
            failed = self.http.post(
                f"/api/phone-numbers/provisioning/{request['id']}/purchase",
                json={"phone_number": "+14165550101"},
            )
            retried = self.http.post(
                f"/api/phone-numbers/provisioning/{request['id']}/purchase"
            )

        self.assertEqual(failed.status_code, 502)
        self.assertEqual(retried.status_code, 200, retried.text)
        purchase.assert_called_once()
        self.assertEqual(configure.call_count, 2)

    def test_completion_database_failure_retries_without_a_second_purchase(self):
        request = self.create_request()
        self.database.fail_rpc = True
        purchased_number = SimpleNamespace(
            phone_number="+14165550101",
            iso_country="CA",
            sid=f"PN{'d' * 32}",
        )
        with (
            patch.object(
                phone_numbers.twilio_client,
                "purchase_canadian_local_number",
                return_value=purchased_number,
            ) as purchase,
            patch.object(
                phone_numbers.twilio_client,
                "configure_twilio_voice",
            ) as configure,
        ):
            failed = self.http.post(
                f"/api/phone-numbers/provisioning/{request['id']}/purchase",
                json={"phone_number": "+14165550101"},
            )
            retried = self.http.post(
                f"/api/phone-numbers/provisioning/{request['id']}/purchase"
            )

        self.assertEqual(failed.status_code, 503)
        self.assertEqual(retried.status_code, 200, retried.text)
        purchase.assert_called_once()
        self.assertEqual(configure.call_count, 2)

    def test_ambiguous_purchase_is_reconciled_without_repurchase(self):
        request = self.create_request()
        purchased_number = SimpleNamespace(
            phone_number="+14165550101",
            iso_country="CA",
            sid=f"PN{'b' * 32}",
        )
        with (
            patch.object(
                phone_numbers.twilio_client,
                "purchase_canadian_local_number",
                side_effect=Timeout("outcome unknown"),
            ) as purchase,
            patch.object(
                phone_numbers.twilio_client,
                "find_provisioned_canadian_number",
                return_value=purchased_number,
            ) as reconcile,
            patch.object(phone_numbers.twilio_client, "configure_twilio_voice"),
        ):
            failed = self.http.post(
                f"/api/phone-numbers/provisioning/{request['id']}/purchase",
                json={"phone_number": "+14165550101"},
            )
            retried_without_reconciliation = self.http.post(
                f"/api/phone-numbers/provisioning/{request['id']}/purchase",
                json={"phone_number": "+14165550101"},
            )
            recovered = self.http.post(
                f"/api/phone-numbers/provisioning/{request['id']}/reconcile"
            )

        self.assertEqual(failed.status_code, 502)
        self.assertEqual(retried_without_reconciliation.status_code, 409)
        self.assertEqual(recovered.status_code, 200, recovered.text)
        purchase.assert_called_once()
        reconcile.assert_called_once_with("+14165550101", request["id"])

    def test_inventory_lookup_failure_is_retryable_without_reconciliation(self):
        request = self.create_request()
        purchased_number = SimpleNamespace(
            phone_number="+14165550101",
            iso_country="CA",
            sid=f"PN{'a' * 32}",
        )
        with (
            patch.object(
                phone_numbers.twilio_client,
                "purchase_canadian_local_number",
                side_effect=[
                    phone_numbers.twilio_client.TwilioInventoryLookupError(),
                    purchased_number,
                ],
            ) as purchase,
            patch.object(phone_numbers.twilio_client, "configure_twilio_voice"),
        ):
            failed = self.http.post(
                f"/api/phone-numbers/provisioning/{request['id']}/purchase",
                json={"phone_number": "+14165550101"},
            )
            failed_state = self.database.tables["phone_number_provisioning"][0][
                "status"
            ]
            retried = self.http.post(
                f"/api/phone-numbers/provisioning/{request['id']}/purchase",
                json={"phone_number": "+14165550101"},
            )

        self.assertEqual(failed.status_code, 502)
        self.assertEqual(failed_state, "purchase_failed")
        self.assertEqual(retried.status_code, 200, retried.text)
        self.assertEqual(purchase.call_count, 2)

    def test_release_requires_confirmation(self):
        self.database.tables["phone_numbers"].append(
            {
                "id": "phone-1",
                "realtor_id": REALTOR_ID,
                "property_id": PROPERTY_ID,
                "phone_number": "+14165550101",
                "twilio_sid": f"PN{'c' * 32}",
                "status": "inactive",
            }
        )
        with patch.object(phone_numbers.twilio_client, "release_twilio_number") as release:
            missing = self.http.post("/api/phone-numbers/phone-1/release")
            false = self.http.post(
                "/api/phone-numbers/phone-1/release",
                json={"confirm_release": False},
            )

        self.assertEqual(missing.status_code, 400)
        self.assertEqual(false.status_code, 400)
        release.assert_not_called()

    def test_deactivation_does_not_release_and_release_requires_inactive(self):
        self.database.tables["phone_numbers"].append(
            {
                "id": "phone-1",
                "realtor_id": REALTOR_ID,
                "property_id": PROPERTY_ID,
                "phone_number": "+14165550101",
                "twilio_sid": f"PN{'c' * 32}",
                "status": "active",
            }
        )
        with patch.object(phone_numbers.twilio_client, "release_twilio_number") as release:
            active_release = self.http.post(
                "/api/phone-numbers/phone-1/release",
                json={"confirm_release": True},
            )
            deactivated = self.http.post(
                "/api/phone-numbers/phone-1/deactivate"
            )
            self.assertEqual(deactivated.status_code, 200, deactivated.text)
            release.assert_not_called()

            released = self.http.post(
                "/api/phone-numbers/phone-1/release",
                json={"confirm_release": True},
            )

        self.assertEqual(active_release.status_code, 409)
        self.assertEqual(released.status_code, 200, released.text)
        release.assert_called_once_with(f"PN{'c' * 32}")
        self.assertEqual(
            self.database.tables["phone_numbers"][0]["status"],
            "inactive",
        )

    def test_frontend_cannot_choose_realtor_identity(self):
        response = self.http.post(
            "/api/phone-numbers/provisioning",
            json={
                "property_id": PROPERTY_ID,
                "idempotency_key": "api-key-1",
                "realtor_id": "other-realtor",
            },
        )

        self.assertEqual(response.status_code, 422)


if __name__ == "__main__":
    unittest.main()
