BEGIN;

DO $preflight$
BEGIN
    IF EXISTS (
        WITH normalized AS (
            SELECT
                realtor_id,
                CASE
                    WHEN phone ~ '\A\+[0-9 ().-]+\Z'
                     AND ('+' || regexp_replace(phone, '[^0-9]', '', 'g'))
                         ~ '\A\+[1-9][0-9]{7,14}\Z'
                    THEN '+' || regexp_replace(phone, '[^0-9]', '', 'g')
                END AS normalized_phone
            FROM public.leads
        )
        SELECT 1
        FROM normalized
        WHERE normalized_phone IS NOT NULL
        GROUP BY realtor_id, normalized_phone
        HAVING COUNT(*) > 1
    ) THEN
        RAISE EXCEPTION
            'Migration aborted: duplicate normalized realtor and lead phone numbers exist; review and resolve duplicates manually';
    END IF;
END
$preflight$;

ALTER TABLE public.leads
    ADD COLUMN intent TEXT NOT NULL DEFAULT 'unknown'
        CHECK (
            intent IN (
                'property_inquiry',
                'viewing_request',
                'buying',
                'selling',
                'financing_question',
                'general_inquiry',
                'unknown'
            )
        ),
    ADD COLUMN lead_type TEXT NOT NULL DEFAULT 'unknown'
        CHECK (lead_type IN ('buyer', 'seller', 'both', 'unknown')),
    ADD COLUMN normalized_phone TEXT GENERATED ALWAYS AS (
        CASE
            WHEN phone ~ '\A\+[0-9 ().-]+\Z'
             AND ('+' || regexp_replace(phone, '[^0-9]', '', 'g'))
                 ~ '\A\+[1-9][0-9]{7,14}\Z'
            THEN '+' || regexp_replace(phone, '[^0-9]', '', 'g')
        END
    ) STORED;

CREATE UNIQUE INDEX leads_realtor_normalized_phone_unique_idx
    ON public.leads (realtor_id, normalized_phone)
    WHERE normalized_phone IS NOT NULL;

COMMIT;
