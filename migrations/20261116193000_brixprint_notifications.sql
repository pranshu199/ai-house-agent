BEGIN;

DO $preflight$
DECLARE
    expected_table TEXT;
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'realtors'
          AND column_name = 'id'
          AND udt_name = 'uuid'
    ) THEN
        RAISE EXCEPTION
            'Migration aborted: public.realtors.id must exist and have UUID type';
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'realtors'
          AND column_name = 'user_id'
          AND udt_name = 'uuid'
    ) THEN
        RAISE EXCEPTION
            'Migration aborted: public.realtors.user_id must exist and have UUID type for realtor-scoped RLS';
    END IF;

    FOREACH expected_table IN ARRAY ARRAY['properties', 'calls', 'leads']
    LOOP
        IF NOT EXISTS (
            SELECT 1
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = expected_table
              AND column_name = 'id'
              AND udt_name = 'uuid'
        ) THEN
            RAISE EXCEPTION
                'Migration aborted: public.%.id must exist and have UUID type',
                expected_table;
        END IF;
    END LOOP;

    IF to_regclass('public.appointments') IS NOT NULL
       AND NOT EXISTS (
            SELECT 1
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = 'appointments'
              AND column_name = 'id'
              AND udt_name = 'uuid'
       ) THEN
        RAISE EXCEPTION
            'Migration aborted: public.appointments.id exists but is not UUID';
    END IF;
END
$preflight$;

CREATE TABLE IF NOT EXISTS public.notification_preferences (
    realtor_id UUID PRIMARY KEY
        REFERENCES public.realtors(id) ON DELETE RESTRICT,
    sms_enabled BOOLEAN NOT NULL DEFAULT TRUE,
    email_enabled BOOLEAN NOT NULL DEFAULT TRUE,
    sms_incoming_calls BOOLEAN NOT NULL DEFAULT TRUE,
    sms_new_leads BOOLEAN NOT NULL DEFAULT TRUE,
    sms_viewing_requests BOOLEAN NOT NULL DEFAULT TRUE,
    email_new_leads BOOLEAN NOT NULL DEFAULT TRUE,
    email_viewing_requests BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS public.notification_deliveries (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    realtor_id UUID NOT NULL
        REFERENCES public.realtors(id) ON DELETE RESTRICT,
    property_id UUID
        REFERENCES public.properties(id) ON DELETE SET NULL,
    call_id UUID
        REFERENCES public.calls(id) ON DELETE SET NULL,
    lead_id UUID
        REFERENCES public.leads(id) ON DELETE SET NULL,
    appointment_id UUID,
    event_type TEXT NOT NULL
        CHECK (
            event_type IN (
                'incoming_call_started',
                'new_lead_captured',
                'viewing_requested'
            )
        ),
    channel TEXT NOT NULL CHECK (channel IN ('sms', 'email')),
    idempotency_key TEXT NOT NULL
        CHECK (length(btrim(idempotency_key)) BETWEEN 1 AND 255),
    delivery_status TEXT NOT NULL DEFAULT 'pending'
        CHECK (delivery_status IN ('pending', 'sending', 'sent', 'delivered', 'failed')),
    provider_message_id TEXT,
    error_message TEXT
        CHECK (error_message IS NULL OR length(error_message) <= 500),
    retry_count INTEGER NOT NULL DEFAULT 0 CHECK (retry_count >= 0),
    next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    sent_at TIMESTAMPTZ,
    delivered_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT notification_deliveries_realtor_idempotency_unique
        UNIQUE (realtor_id, idempotency_key)
);

CREATE INDEX IF NOT EXISTS notification_deliveries_retry_idx
    ON public.notification_deliveries (next_attempt_at, created_at)
    WHERE delivery_status = 'pending';

CREATE INDEX IF NOT EXISTS notification_deliveries_stale_send_idx
    ON public.notification_deliveries (updated_at)
    WHERE delivery_status = 'sending';

CREATE INDEX IF NOT EXISTS notification_deliveries_realtor_created_idx
    ON public.notification_deliveries (realtor_id, created_at DESC);

CREATE INDEX IF NOT EXISTS notification_deliveries_call_idx
    ON public.notification_deliveries (call_id)
    WHERE call_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS notification_deliveries_lead_idx
    ON public.notification_deliveries (lead_id)
    WHERE lead_id IS NOT NULL;

DO $appointment_fk$
BEGIN
    IF to_regclass('public.appointments') IS NOT NULL
       AND NOT EXISTS (
            SELECT 1
            FROM pg_constraint
            WHERE conrelid = 'public.notification_deliveries'::regclass
              AND conname = 'notification_deliveries_appointment_id_fkey'
       ) THEN
        ALTER TABLE public.notification_deliveries
            ADD CONSTRAINT notification_deliveries_appointment_id_fkey
            FOREIGN KEY (appointment_id)
            REFERENCES public.appointments(id)
            ON DELETE SET NULL;
    END IF;
END
$appointment_fk$;

ALTER TABLE public.notification_preferences ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.notification_deliveries ENABLE ROW LEVEL SECURITY;

DO $policies$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE schemaname = 'public'
          AND tablename = 'notification_preferences'
          AND policyname = 'notification_preferences_select_own'
    ) THEN
        CREATE POLICY notification_preferences_select_own
            ON public.notification_preferences
            FOR SELECT TO authenticated
            USING (
                EXISTS (
                    SELECT 1
                    FROM public.realtors AS r
                    WHERE r.id = realtor_id
                      AND r.user_id = auth.uid()
                )
            );
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE schemaname = 'public'
          AND tablename = 'notification_preferences'
          AND policyname = 'notification_preferences_insert_own'
    ) THEN
        CREATE POLICY notification_preferences_insert_own
            ON public.notification_preferences
            FOR INSERT TO authenticated
            WITH CHECK (
                EXISTS (
                    SELECT 1
                    FROM public.realtors AS r
                    WHERE r.id = realtor_id
                      AND r.user_id = auth.uid()
                )
            );
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE schemaname = 'public'
          AND tablename = 'notification_preferences'
          AND policyname = 'notification_preferences_update_own'
    ) THEN
        CREATE POLICY notification_preferences_update_own
            ON public.notification_preferences
            FOR UPDATE TO authenticated
            USING (
                EXISTS (
                    SELECT 1
                    FROM public.realtors AS r
                    WHERE r.id = realtor_id
                      AND r.user_id = auth.uid()
                )
            )
            WITH CHECK (
                EXISTS (
                    SELECT 1
                    FROM public.realtors AS r
                    WHERE r.id = realtor_id
                      AND r.user_id = auth.uid()
                )
            );
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE schemaname = 'public'
          AND tablename = 'notification_deliveries'
          AND policyname = 'notification_deliveries_select_own'
    ) THEN
        CREATE POLICY notification_deliveries_select_own
            ON public.notification_deliveries
            FOR SELECT TO authenticated
            USING (
                EXISTS (
                    SELECT 1
                    FROM public.realtors AS r
                    WHERE r.id = realtor_id
                      AND r.user_id = auth.uid()
                )
            );
    END IF;
END
$policies$;

GRANT SELECT, INSERT, UPDATE
    ON public.notification_preferences
    TO authenticated;
GRANT SELECT
    ON public.notification_deliveries
    TO authenticated;
GRANT ALL
    ON public.notification_preferences, public.notification_deliveries
    TO service_role;

COMMENT ON COLUMN public.notification_deliveries.appointment_id IS
    'Optional reference; an FK is added only when public.appointments(id) exists with UUID type.';

COMMIT;
