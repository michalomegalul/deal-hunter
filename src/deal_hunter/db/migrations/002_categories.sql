-- More than GPUs: listings remember which Bazoš section they came from, and the normalizer output
-- becomes category + model (the pricing key) + features, shared by GPUs and ex-office hardware.

ALTER TABLE listings ADD COLUMN section text NOT NULL DEFAULT 'graficka';  -- graficka | pc | notebook
CREATE INDEX listings_section_idx ON listings (section, first_seen);

ALTER TABLE normalized ADD COLUMN category text;   -- gpu | office | office_pc | office_laptop
ALTER TABLE normalized ADD COLUMN model text;      -- 'rtx 3060 12gb', 'thinkpad t480', 'optiplex 7060 micro'
ALTER TABLE normalized ADD COLUMN features jsonb NOT NULL DEFAULT '{}';  -- vram_gb | ram_gb, ssd_gb, cpu
UPDATE normalized SET category = 'gpu', model = gpu_model,
    features = CASE WHEN vram_gb IS NULL THEN '{}'::jsonb ELSE jsonb_build_object('vram_gb', vram_gb) END;
ALTER TABLE normalized ALTER COLUMN category SET NOT NULL;
DROP INDEX normalized_model_idx;
ALTER TABLE normalized DROP COLUMN gpu_model;
ALTER TABLE normalized DROP COLUMN vram_gb;
CREATE INDEX normalized_model_idx ON normalized (model) WHERE model IS NOT NULL;
