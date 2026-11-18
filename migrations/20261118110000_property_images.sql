-- Create property_images table with RLS and indexes
-- Stores metadata for property images in Supabase Storage
-- Storage path format: {realtor_id}/{property_id}/{uuid}.{extension}

BEGIN;

-- Create the property_images table
CREATE TABLE public.property_images (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    property_id UUID NOT NULL
        REFERENCES public.properties(id) ON DELETE CASCADE,
    realtor_id UUID NOT NULL
        REFERENCES public.realtors(id) ON DELETE CASCADE,
    storage_path TEXT NOT NULL
        UNIQUE
        CHECK (
            length(btrim(storage_path)) > 0
            AND storage_path ~ '^[a-f0-9\-]{36}/[a-f0-9\-]{36}/[a-f0-9\-]{36}\.(jpg|jpeg|png|webp)$'
        ),
    is_primary BOOLEAN NOT NULL DEFAULT FALSE,
    sort_order INTEGER NOT NULL DEFAULT 0
        CHECK (sort_order >= 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT property_images_property_realtor_match
        CHECK (
            (property_id, realtor_id) IN (
                SELECT id, realtor_id FROM public.properties WHERE realtor_id = property_images.realtor_id
            )
        )
);

-- Unique partial index: at most one primary image per property
CREATE UNIQUE INDEX property_images_one_primary_per_property_idx
    ON public.property_images (property_id)
    WHERE is_primary = TRUE;

-- Indexes for common queries
CREATE INDEX property_images_property_idx
    ON public.property_images (property_id);

CREATE INDEX property_images_realtor_idx
    ON public.property_images (realtor_id);

CREATE INDEX property_images_sort_order_idx
    ON public.property_images (property_id, sort_order ASC, created_at DESC);

-- Enable RLS
ALTER TABLE public.property_images ENABLE ROW LEVEL SECURITY;

-- RLS Policy: Authenticated users can read images for properties they own
CREATE POLICY property_images_read_own_realtor_properties
    ON public.property_images
    FOR SELECT
    TO authenticated
    USING (
        realtor_id = (
            SELECT id FROM public.realtors
            WHERE user_id = auth.uid()
            LIMIT 1
        )
    );

-- RLS Policy: Authenticated users can insert images for properties they own
CREATE POLICY property_images_insert_own_realtor_properties
    ON public.property_images
    FOR INSERT
    TO authenticated
    WITH CHECK (
        realtor_id = (
            SELECT id FROM public.realtors
            WHERE user_id = auth.uid()
            LIMIT 1
        )
        AND property_id IN (
            SELECT id FROM public.properties
            WHERE realtor_id = (
                SELECT id FROM public.realtors
                WHERE user_id = auth.uid()
                LIMIT 1
            )
        )
    );

-- RLS Policy: Authenticated users can update images (primary, sort_order) for properties they own
CREATE POLICY property_images_update_own_realtor_properties
    ON public.property_images
    FOR UPDATE
    TO authenticated
    USING (
        realtor_id = (
            SELECT id FROM public.realtors
            WHERE user_id = auth.uid()
            LIMIT 1
        )
    )
    WITH CHECK (
        realtor_id = (
            SELECT id FROM public.realtors
            WHERE user_id = auth.uid()
            LIMIT 1
        )
        AND property_id IN (
            SELECT id FROM public.properties
            WHERE realtor_id = (
                SELECT id FROM public.realtors
                WHERE user_id = auth.uid()
                LIMIT 1
            )
        )
    );

-- RLS Policy: Authenticated users can delete images for properties they own
CREATE POLICY property_images_delete_own_realtor_properties
    ON public.property_images
    FOR DELETE
    TO authenticated
    USING (
        realtor_id = (
            SELECT id FROM public.realtors
            WHERE user_id = auth.uid()
            LIMIT 1
        )
    );

-- Service role (backend) can access all images
CREATE POLICY property_images_service_role_all
    ON public.property_images
    FOR ALL
    TO service_role
    USING (TRUE)
    WITH CHECK (TRUE);

-- Trigger to update updated_at on modifications
CREATE TRIGGER property_images_update_updated_at_trigger
    BEFORE UPDATE ON public.property_images
    FOR EACH ROW
    EXECUTE FUNCTION public.update_updated_at_column();

-- Grant permissions
GRANT SELECT, INSERT, UPDATE, DELETE ON public.property_images TO authenticated;
GRANT ALL ON public.property_images TO service_role;

-- Add property_images to realtime publications
ALTER PUBLICATION supabase_realtime ADD TABLE public.property_images;

COMMIT;
