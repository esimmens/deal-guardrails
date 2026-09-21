-- Append-only, hash-chained audit log. Tamper-evident, not tamper-proof: a superuser can
-- disable the trigger, and the chain still catches the edit.
CREATE TABLE audit_events (
  seq         bigserial PRIMARY KEY,
  deal_id     bigint REFERENCES deals(id),
  event_type  text NOT NULL,
  actor       jsonb NOT NULL,
  payload     jsonb NOT NULL,
  occurred_at timestamptz NOT NULL,
  canonical   text NOT NULL,      -- the exact bytes that were hashed, so a verifier can re-derive
  prev_hash   text NOT NULL,
  row_hash    text NOT NULL UNIQUE
);
CREATE INDEX audit_events_deal_idx ON audit_events (deal_id, seq);

CREATE FUNCTION audit_events_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'audit_events is append-only (% blocked)', TG_OP;
END $$;
CREATE TRIGGER audit_events_immutable BEFORE UPDATE OR DELETE ON audit_events
  FOR EACH ROW EXECUTE FUNCTION audit_events_immutable();
