-- Phase 6: the flips log grows selling costs, notes and a category, so `hunt flip` / `hunt eval` can show profit.

ALTER TABLE flips ADD COLUMN fees integer;        -- Kč: postage, cleaning, parts; profit = sold - buy - fees
ALTER TABLE flips ADD COLUMN notes text;
ALTER TABLE flips ADD COLUMN category text;       -- gpu | office_pc | office_laptop | hdd | ...; NULL = unknown

-- Flips already logged by "Bought ✅" get the category of their ad, when it was normalized.
UPDATE flips f SET category = n.category FROM normalized n WHERE n.listing_id = f.listing_id AND f.category IS NULL;
