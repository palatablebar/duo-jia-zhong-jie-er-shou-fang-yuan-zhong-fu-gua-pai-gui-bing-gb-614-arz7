-- 核心表结构参考（MySQL 8.0 语法，运行时不执行；建表由 SQLAlchemy create_all 完成）

CREATE TABLE users (
    id              INT AUTO_INCREMENT PRIMARY KEY,
    email           VARCHAR(160) NOT NULL UNIQUE,
    password_hash   VARCHAR(255) NOT NULL,
    display_name    VARCHAR(80)  NOT NULL,
    role            VARCHAR(20)  NOT NULL,
    org             VARCHAR(120),
    created_at      DATETIME     NOT NULL
);

CREATE TABLE agencies (
    id              INT AUTO_INCREMENT PRIMARY KEY,
    code            VARCHAR(40)  NOT NULL UNIQUE,
    name            VARCHAR(120) NOT NULL,
    kind            VARCHAR(20)  NOT NULL,           -- broker / platform / feed
    endpoint        VARCHAR(255),
    owner           VARCHAR(80),
    note            TEXT,
    config          JSON,
    enabled         BOOLEAN      NOT NULL DEFAULT TRUE,
    last_synced_at  DATETIME,
    created_at      DATETIME     NOT NULL,
    updated_at      DATETIME     NOT NULL
);

-- 整批快照接入：保留完整历史，自然键 (agency_id, batch_key) 保证重试幂等
CREATE TABLE snapshot_batches (
    id              INT AUTO_INCREMENT PRIMARY KEY,
    agency_id       INT          NOT NULL,
    batch_key       VARCHAR(120) NOT NULL,           -- 采集侧批次唯一标识
    snapshot_date   DATE         NOT NULL,           -- 批次归属快照日（判定晚到旧批）
    status          VARCHAR(20)  NOT NULL DEFAULT 'applied', -- applied / stale / duplicate
    row_count       INT          NOT NULL DEFAULT 0,
    summary         JSON,
    note            TEXT,
    received_at     DATETIME     NOT NULL,
    applied_at      DATETIME,
    created_at      DATETIME     NOT NULL,
    updated_at      DATETIME     NOT NULL,
    CONSTRAINT uq_snapshot_batches_agency_batch UNIQUE (agency_id, batch_key),
    CONSTRAINT fk_snapshot_batches_agency FOREIGN KEY (agency_id) REFERENCES agencies(id) ON DELETE CASCADE
);

-- 批次内每条挂牌原文：只追加不覆盖，是对账与失效依据的留痕来源
CREATE TABLE listing_snapshots (
    id               INT AUTO_INCREMENT PRIMARY KEY,
    batch_id         INT          NOT NULL,
    agency_id        INT          NOT NULL,
    lid              VARCHAR(40)  NOT NULL,
    community_raw    VARCHAR(160) NOT NULL,
    layout_raw       VARCHAR(60)  NOT NULL,
    area_raw         VARCHAR(60)  NOT NULL,
    price_raw        VARCHAR(60)  NOT NULL,
    floor_raw        VARCHAR(60)  NOT NULL,
    orientation_raw  VARCHAR(40)  NOT NULL,
    title            VARCHAR(200),
    status           VARCHAR(20)  NOT NULL DEFAULT 'active',
    listed_days      INT          NOT NULL DEFAULT 0,
    views            INT          NOT NULL DEFAULT 0,
    captured_at      DATETIME     NOT NULL,
    raw_payload      JSON,
    created_at       DATETIME     NOT NULL,
    CONSTRAINT uq_listing_snapshots_batch_lid UNIQUE (batch_id, lid),
    CONSTRAINT fk_listing_snapshots_batch FOREIGN KEY (batch_id) REFERENCES snapshot_batches(id) ON DELETE CASCADE,
    CONSTRAINT fk_listing_snapshots_agency FOREIGN KEY (agency_id) REFERENCES agencies(id) ON DELETE CASCADE
);

CREATE TABLE match_runs (
    id              INT AUTO_INCREMENT PRIMARY KEY,
    run_no          VARCHAR(40)  NOT NULL UNIQUE,
    `trigger`       VARCHAR(20)  NOT NULL,           -- manual / schedule / seed
    status          VARCHAR(20)  NOT NULL,
    operator_email  VARCHAR(160) NOT NULL,
    listing_count   INT          NOT NULL DEFAULT 0,
    cluster_count   INT          NOT NULL DEFAULT 0,
    multi_count     INT          NOT NULL DEFAULT 0,
    dedup_rate      DOUBLE       NOT NULL DEFAULT 0,
    risk_count      INT          NOT NULL DEFAULT 0,
    threshold       DOUBLE       NOT NULL DEFAULT 0.8,
    metrics         JSON,
    log_text        TEXT,
    seed_key        VARCHAR(120) UNIQUE,
    started_at      DATETIME     NOT NULL,
    finished_at     DATETIME
);

CREATE TABLE listings (
    id               INT AUTO_INCREMENT PRIMARY KEY,
    agency_id        INT          NOT NULL,
    lid              VARCHAR(40)  NOT NULL,
    community_raw    VARCHAR(160) NOT NULL,
    layout_raw       VARCHAR(60)  NOT NULL,
    area_raw         VARCHAR(60)  NOT NULL,
    price_raw        VARCHAR(60)  NOT NULL,
    floor_raw        VARCHAR(60)  NOT NULL,
    orientation_raw  VARCHAR(40)  NOT NULL,
    title            VARCHAR(200),
    status           VARCHAR(20)  NOT NULL DEFAULT 'active',
    listed_days      INT          NOT NULL DEFAULT 0,
    views            INT          NOT NULL DEFAULT 0,
    captured_at      DATETIME     NOT NULL,
    community_norm   VARCHAR(160),
    layout_norm      VARCHAR(40),
    area_num         DOUBLE,
    price_num        INT,
    floor_num        INT,
    floor_total      INT,
    floor_band       VARCHAR(10),
    orientation_norm VARCHAR(20),
    title_tokens     JSON,
    parse_flags      JSON,
    property_id      INT,
    risk_tags        JSON,
    -- 快照生命周期：只有 is_current=TRUE 的当前有效挂牌进入归并
    is_current       BOOLEAN      NOT NULL DEFAULT TRUE,
    first_seen_batch_id INT,
    last_seen_batch_id  INT,
    invalid_batch_id    INT,
    invalid_reason   VARCHAR(20),                  -- withdrawn / off_market / sold / stale_batch
    invalid_evidence JSON,
    invalidated_at   DATETIME,
    created_at       DATETIME     NOT NULL,
    updated_at       DATETIME     NOT NULL,
    CONSTRAINT uq_listings_agency_lid UNIQUE (agency_id, lid),
    CONSTRAINT fk_listings_agency FOREIGN KEY (agency_id) REFERENCES agencies(id) ON DELETE CASCADE,
    CONSTRAINT fk_listings_first_batch FOREIGN KEY (first_seen_batch_id) REFERENCES snapshot_batches(id) ON DELETE SET NULL,
    CONSTRAINT fk_listings_last_batch FOREIGN KEY (last_seen_batch_id) REFERENCES snapshot_batches(id) ON DELETE SET NULL,
    CONSTRAINT fk_listings_invalid_batch FOREIGN KEY (invalid_batch_id) REFERENCES snapshot_batches(id) ON DELETE SET NULL
);

CREATE TABLE properties (
    id           INT AUTO_INCREMENT PRIMARY KEY,
    cluster_key  VARCHAR(64)  NOT NULL UNIQUE,
    community    VARCHAR(160) NOT NULL,
    layout       VARCHAR(40)  NOT NULL,
    area_mode    DOUBLE,
    area_min     DOUBLE,
    area_max     DOUBLE,
    price_median INT,
    price_min    INT,
    price_max    INT,
    price_p10    INT,
    price_p90    INT,
    spread       INT,
    dispersion   DOUBLE,
    agency_count INT,
    member_count INT,
    confidence   DOUBLE,
    risk_score   INT          NOT NULL DEFAULT 0,
    risk_level   VARCHAR(10)  NOT NULL DEFAULT 'none',
    risk_tags    JSON,
    risk_detail  JSON,
    run_id       INT,
    seeded_at    DATETIME     NOT NULL,
    created_at   DATETIME     NOT NULL,
    updated_at   DATETIME     NOT NULL,
    CONSTRAINT fk_properties_run FOREIGN KEY (run_id) REFERENCES match_runs(id) ON DELETE SET NULL
);

ALTER TABLE listings
    ADD CONSTRAINT fk_listings_property FOREIGN KEY (property_id) REFERENCES properties(id) ON DELETE SET NULL;

CREATE TABLE reviews (
    id          INT AUTO_INCREMENT PRIMARY KEY,
    property_id INT          NOT NULL,
    run_id      INT,
    reason      VARCHAR(20)  NOT NULL,              -- bait / stale / spread / manual
    severity    VARCHAR(10)  NOT NULL,
    risk_score  INT          NOT NULL DEFAULT 0,
    status      VARCHAR(16)  NOT NULL DEFAULT 'pending',
    reviewer    VARCHAR(160),
    note        TEXT,
    seed_key    VARCHAR(120) UNIQUE,
    created_at  DATETIME     NOT NULL,
    updated_at  DATETIME     NOT NULL,
    CONSTRAINT fk_reviews_property FOREIGN KEY (property_id) REFERENCES properties(id) ON DELETE CASCADE,
    CONSTRAINT fk_reviews_run FOREIGN KEY (run_id) REFERENCES match_runs(id) ON DELETE SET NULL
);

CREATE TABLE audit_logs (
    id              INT AUTO_INCREMENT PRIMARY KEY,
    actor_email     VARCHAR(160) NOT NULL,
    actor_role      VARCHAR(20)  NOT NULL,
    action          VARCHAR(64)  NOT NULL,
    entity          VARCHAR(40)  NOT NULL,
    entity_id       VARCHAR(64),
    detail          JSON,
    layer           VARCHAR(16)  NOT NULL DEFAULT 'domain',
    ip              VARCHAR(64),
    seed_key        VARCHAR(120) UNIQUE,
    created_at      DATETIME     NOT NULL
);
