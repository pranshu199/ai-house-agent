BEGIN;

DO $preflight$
BEGIN
    IF EXISTS (
        SELECT property_id
        FROM public.phone_numbers
        WHERE status = 'active'
          AND property_id IS NOT NULL
        GROUP BY property_id
        HAVING COUNT(*) > 1
    ) THEN
        RAISE EXCEPTION
            'Migration aborted: duplicate active phone numbers exist for a property';
    END IF;
END
$preflight$;

ALTER TABLE public.phone_numbers
    ALTER COLUMN campaign_id DROP NOT NULL;

CREATE TABLE public.phone_number_provisioning (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    realtor_id UUID NOT NULL
        REFERENCES public.realtors(id) ON DELETE RESTRICT,
    property_id UUID NOT NULL
        REFERENCES public.properties(id) ON DELETE RESTRICT,
    requested_by UUID NOT NULL
        REFERENCES auth.users(id) ON DELETE RESTRICT,
    idempotency_key TEXT NOT NULL
        CHECK (length(btrim(idempotency_key)) BETWEEN 1 AND 255),
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (
            status IN (
                'pending',
                'purchase_in_progress',
                'purchase_failed',
                'purchased',
                'configuration_in_progress',
                'configuration_failed',
                'completed',
                'cancelled'
            )
        ),
    requested_area_code TEXT
        CHECK (
            requested_area_code IS NULL
            OR requested_area_code ~ '^[2-9][0-9]{2}$'
        ),
    requested_locality TEXT
        CHECK (
            requested_locality IS NULL
            OR length(requested_locality) BETWEEN 1 AND 60
        ),
    phone_number TEXT
        CHECK (
            phone_number IS NULL
            OR phone_number ~ '^\+1[2-9][0-9]{9}$'
        ),
    twilio_sid TEXT
        CHECK (
            twilio_sid IS NULL
            OR twilio_sid ~ '^PN[0-9A-Fa-f]{32}$'
        ),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    last_error_code TEXT
        CHECK (
            last_error_code IS NULL
            OR last_error_code ~ '^[A-Za-z0-9_.-]{1,64}$'
        ),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ,
    CONSTRAINT phone_number_provisioning_completion_check
        CHECK ((status = 'completed') = (completed_at IS NOT NULL)),
    CONSTRAINT phone_number_provisioning_purchase_result_check
        CHECK (
            status NOT IN (
                'purchased',
                'configuration_in_progress',
                'configuration_failed',
                'completed'
            )
            OR (phone_number IS NOT NULL AND twilio_sid IS NOT NULL)
        ),
    CONSTRAINT phone_number_provisioning_idempotency_unique
        UNIQUE (realtor_id, idempotency_key)
);

CREATE INDEX phone_number_provisioning_realtor_created_idx
    ON public.phone_number_provisioning (realtor_id, created_at DESC);

CREATE INDEX phone_number_provisioning_property_created_idx
    ON public.phone_number_provisioning (property_id, created_at DESC);

CREATE INDEX phone_number_provisioning_recovery_idx
    ON public.phone_number_provisioning (status, updated_at)
    WHERE status IN (
        'pending',
        'purchase_in_progress',
        'purchase_failed',
        'purchased',
        'configuration_in_progress',
        'configuration_failed'
    );

CREATE UNIQUE INDEX phone_number_provisioning_one_open_property_idx
    ON public.phone_number_provisioning (property_id)
    WHERE status IN (
        'pending',
        'purchase_in_progress',
        'purchase_failed',
        'purchased',
        'configuration_in_progress',
        'configuration_failed'
    );

CREATE UNIQUE INDEX phone_numbers_one_active_per_property_idx
    ON public.phone_numbers (property_id)
    WHERE status = 'active'
      AND property_id IS NOT NULL;

CREATE FUNCTION public.enforce_phone_number_provisioning_consistency()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $function$
DECLARE
    property_realtor_id UUID;
BEGIN
    SELECT p.realtor_id
    INTO property_realtor_id
    FROM public.properties AS p
    WHERE p.id = NEW.property_id;

    IF property_realtor_id IS NULL
       OR property_realtor_id <> NEW.realtor_id THEN
        RAISE EXCEPTION
            'Provisioning property must belong to the requested realtor'
            USING ERRCODE = '23514';
    END IF;

    NEW.updated_at = now();
    RETURN NEW;
END;
$function$;

CREATE TRIGGER phone_number_provisioning_consistency_trigger
    BEFORE INSERT OR UPDATE OF realtor_id, property_id, status
    ON public.phone_number_provisioning
    FOR EACH ROW
    EXECUTE FUNCTION public.enforce_phone_number_provisioning_consistency();

CREATE FUNCTION public.user_owns_phone_realtor(p_realtor_id UUID)
RETURNS BOOLEAN
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $function$
    SELECT EXISTS (
        SELECT 1
        FROM public.realtors AS r
        WHERE r.id = p_realtor_id
          AND r.user_id = auth.uid()
    );
$function$;

CREATE FUNCTION public.complete_phone_number_provisioning(
    p_request_id UUID,
    p_realtor_id UUID
)
RETURNS TABLE (
    provisioning_request_id UUID,
    assigned_phone_number_id UUID
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $function$
DECLARE
    request_record public.phone_number_provisioning%ROWTYPE;
    v_phone_number_id UUID;
BEGIN
    SELECT *
    INTO request_record
    FROM public.phone_number_provisioning
    WHERE id = p_request_id
      AND realtor_id = p_realtor_id
    FOR UPDATE;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'Provisioning request not found'
            USING ERRCODE = 'P0002';
    END IF;

    IF request_record.status = 'completed' THEN
        SELECT pn.id
        INTO v_phone_number_id
        FROM public.phone_numbers AS pn
        WHERE pn.twilio_sid = request_record.twilio_sid
          AND pn.property_id = request_record.property_id
          AND pn.realtor_id = request_record.realtor_id
          AND pn.status = 'active';

        IF v_phone_number_id IS NULL THEN
            RAISE EXCEPTION 'Completed provisioning request has no active assignment'
                USING ERRCODE = '23514';
        END IF;

        RETURN QUERY SELECT p_request_id, v_phone_number_id;
        RETURN;
    END IF;

    IF request_record.status <> 'configuration_in_progress'
       OR request_record.phone_number IS NULL
       OR request_record.twilio_sid IS NULL THEN
        RAISE EXCEPTION 'Provisioning request is not ready for assignment'
            USING ERRCODE = '23514';
    END IF;

    INSERT INTO public.phone_numbers (
        phone_number,
        twilio_sid,
        realtor_id,
        property_id,
        campaign_id,
        status
    )
    VALUES (
        request_record.phone_number,
        request_record.twilio_sid,
        request_record.realtor_id,
        request_record.property_id,
        NULL,
        'active'
    )
    RETURNING id INTO v_phone_number_id;

    UPDATE public.phone_number_provisioning
    SET status = 'completed',
        completed_at = now(),
        last_error_code = NULL,
        updated_at = now()
    WHERE id = request_record.id;

    RETURN QUERY SELECT p_request_id, v_phone_number_id;
END;
$function$;

ALTER TABLE public.phone_number_provisioning ENABLE ROW LEVEL SECURITY;

CREATE POLICY phone_number_provisioning_select_own
    ON public.phone_number_provisioning
    FOR SELECT
    TO authenticated
    USING (public.user_owns_phone_realtor(realtor_id));

GRANT SELECT
    ON public.phone_number_provisioning
    TO authenticated;

GRANT ALL
    ON public.phone_number_provisioning
    TO service_role;

REVOKE ALL
    ON FUNCTION public.complete_phone_number_provisioning(UUID, UUID)
    FROM PUBLIC, anon, authenticated;

REVOKE ALL
    ON FUNCTION public.user_owns_phone_realtor(UUID)
    FROM PUBLIC, anon;

GRANT EXECUTE
    ON FUNCTION public.user_owns_phone_realtor(UUID)
    TO authenticated, service_role;

GRANT EXECUTE
    ON FUNCTION public.complete_phone_number_provisioning(UUID, UUID)
    TO service_role;

COMMIT;
