-- 离线补传批次、修订合并与隔离重放（在 001_initial.sql 之后执行，幂等）
-- 班次身份改为 港站 + 班次编号 + 上海计划日；同编号跨站/跨日不再冲突。

ALTER TABLE public.arrivals ADD COLUMN IF NOT EXISTS planned_day character varying(10);
ALTER TABLE public.arrivals ADD COLUMN IF NOT EXISTS source_revision integer NOT NULL DEFAULT 0;
ALTER TABLE public.arrivals ADD COLUMN IF NOT EXISTS source_content character varying(64);
ALTER TABLE public.arrivals ADD COLUMN IF NOT EXISTS revoked boolean NOT NULL DEFAULT false;
ALTER TABLE public.arrivals ADD COLUMN IF NOT EXISTS revoked_revision integer;
ALTER TABLE public.arrivals ADD COLUMN IF NOT EXISTS revoked_at character varying(40);
ALTER TABLE public.arrivals ADD COLUMN IF NOT EXISTS source character varying(40) DEFAULT 'manual';
ALTER TABLE public.arrivals ADD COLUMN IF NOT EXISTS last_batch_id integer;
ALTER TABLE public.arrivals ADD COLUMN IF NOT EXISTS updated_at character varying(40);

UPDATE public.arrivals
SET planned_day = (planned_at::timestamptz AT TIME ZONE 'Asia/Shanghai')::date::text
WHERE planned_day IS NULL;

ALTER TABLE public.arrivals ALTER COLUMN planned_day SET NOT NULL;
ALTER TABLE public.arrivals DROP CONSTRAINT IF EXISTS arrivals_code_key;
ALTER TABLE public.arrivals DROP CONSTRAINT IF EXISTS uq_arrival_station_code_day;
DO $$ BEGIN
 IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='uq_arrival_station_code_day') THEN
  ALTER TABLE public.arrivals ADD CONSTRAINT uq_arrival_station_code_day UNIQUE (station, code, planned_day);
 END IF;
END $$;
CREATE INDEX IF NOT EXISTS ix_arrivals_planned_day ON public.arrivals (planned_day);

CREATE TABLE IF NOT EXISTS public.arrival_batches (
    id integer NOT NULL,
    batch_key character varying(100) NOT NULL,
    source character varying(80),
    status character varying(20) NOT NULL DEFAULT 'receiving',
    input_count integer NOT NULL DEFAULT 0,
    applied_count integer NOT NULL DEFAULT 0,
    stale_count integer NOT NULL DEFAULT 0,
    duplicate_count integer NOT NULL DEFAULT 0,
    quarantined_count integer NOT NULL DEFAULT 0,
    payload_hash character varying(64) NOT NULL,
    created_by character varying(100),
    created_at character varying(40),
    completed_at character varying(40)
);
CREATE SEQUENCE IF NOT EXISTS public.arrival_batches_id_seq AS integer START WITH 1 INCREMENT BY 1 NO MINVALUE NO MAXVALUE CACHE 1;
ALTER SEQUENCE public.arrival_batches_id_seq OWNED BY public.arrival_batches.id;
ALTER TABLE ONLY public.arrival_batches ALTER COLUMN id SET DEFAULT nextval('public.arrival_batches_id_seq'::regclass);
DO $$ BEGIN
 IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='arrival_batches_pkey') THEN
  ALTER TABLE ONLY public.arrival_batches ADD CONSTRAINT arrival_batches_pkey PRIMARY KEY (id);
 END IF;
END $$;
DO $$ BEGIN
 IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='arrival_batches_batch_key_key') THEN
  ALTER TABLE ONLY public.arrival_batches ADD CONSTRAINT arrival_batches_batch_key_key UNIQUE (batch_key);
 END IF;
END $$;

CREATE TABLE IF NOT EXISTS public.arrival_batch_items (
    id integer NOT NULL,
    batch_id integer NOT NULL,
    row_no integer NOT NULL,
    raw json NOT NULL,
    normalized json,
    status character varying(20) NOT NULL DEFAULT 'pending',
    reason text,
    arrival_id integer,
    station character varying(30),
    code character varying(60),
    planned_day character varying(10),
    source_revision integer,
    revoked_request boolean NOT NULL DEFAULT false,
    replay_of_item_id integer,
    resolved_by_item_id integer,
    resolution_status character varying(20),
    resolved_at character varying(40),
    processed_at character varying(40)
);
CREATE SEQUENCE IF NOT EXISTS public.arrival_batch_items_id_seq AS integer START WITH 1 INCREMENT BY 1 NO MINVALUE NO MAXVALUE CACHE 1;
ALTER SEQUENCE public.arrival_batch_items_id_seq OWNED BY public.arrival_batch_items.id;
ALTER TABLE ONLY public.arrival_batch_items ALTER COLUMN id SET DEFAULT nextval('public.arrival_batch_items_id_seq'::regclass);
DO $$ BEGIN
 IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='arrival_batch_items_pkey') THEN
  ALTER TABLE ONLY public.arrival_batch_items ADD CONSTRAINT arrival_batch_items_pkey PRIMARY KEY (id);
 END IF;
END $$;
DO $$ BEGIN
 IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='uq_batch_row') THEN
  ALTER TABLE ONLY public.arrival_batch_items ADD CONSTRAINT uq_batch_row UNIQUE (batch_id, row_no);
 END IF;
END $$;
DO $$ BEGIN
 IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='arrival_batch_items_batch_id_fkey') THEN
  ALTER TABLE ONLY public.arrival_batch_items ADD CONSTRAINT arrival_batch_items_batch_id_fkey FOREIGN KEY (batch_id) REFERENCES public.arrival_batches(id);
 END IF;
END $$;
CREATE INDEX IF NOT EXISTS ix_batch_items_status ON public.arrival_batch_items (status);
CREATE INDEX IF NOT EXISTS ix_batch_items_identity ON public.arrival_batch_items (station, code, planned_day);
CREATE INDEX IF NOT EXISTS ix_batch_items_replay_of ON public.arrival_batch_items (replay_of_item_id);

CREATE TABLE IF NOT EXISTS public.arrival_tombstones (
    id integer NOT NULL,
    station character varying(30) NOT NULL,
    code character varying(60) NOT NULL,
    planned_day character varying(10) NOT NULL,
    revoked_revision integer NOT NULL,
    revoked_at character varying(40),
    source character varying(80),
    content_hash character varying(64),
    last_batch_id integer,
    last_item_id integer
);
CREATE SEQUENCE IF NOT EXISTS public.arrival_tombstones_id_seq AS integer START WITH 1 INCREMENT BY 1 NO MINVALUE NO MAXVALUE CACHE 1;
ALTER SEQUENCE public.arrival_tombstones_id_seq OWNED BY public.arrival_tombstones.id;
ALTER TABLE ONLY public.arrival_tombstones ALTER COLUMN id SET DEFAULT nextval('public.arrival_tombstones_id_seq'::regclass);
DO $$ BEGIN
 IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='arrival_tombstones_pkey') THEN
  ALTER TABLE ONLY public.arrival_tombstones ADD CONSTRAINT arrival_tombstones_pkey PRIMARY KEY (id);
 END IF;
END $$;
DO $$ BEGIN
 IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='uq_tombstone_identity') THEN
  ALTER TABLE ONLY public.arrival_tombstones ADD CONSTRAINT uq_tombstone_identity UNIQUE (station, code, planned_day);
 END IF;
END $$;

-- 历史行来源修订未知，保留 source_revision = 0 且 source_content 为空：
-- 任何 >=1 的离线修订可直接生效；若同为修订 0，则明确隔离为“缺少内容指纹”而不是猜测内容一致。
