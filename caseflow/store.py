"""Transactional domain boundaries and an at-least-once local refund simulator.

SQLite is the source of truth. Each operation opens and closes its own connection;
BEGIN IMMEDIATE serializes business writes across HTTP worker threads. Run messages
belong to the runtime; tools write evidence and business data without changing its
CAS revision. Run one server process per database (restart recovery assumes this).
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3
import uuid

from .fixtures import CASES, ORDERS, POLICIES


class DomainError(Exception):
    """Only safe, actionable messages should cross the HTTP/model boundary."""

    def __init__(self, code: str, detail: str | None = None):
        self.code = code
        self.detail = detail or code
        super().__init__(self.detail)


class Conflict(DomainError):
    """A concurrent write or a stale approval requires fresh state."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _tokens(text: str) -> list[str]:
    # Chinese characters and Latin words: transparent, small-corpus lexical retrieval.
    return re.findall(r"[a-z0-9]+|[\u4e00-\u9fff]", text.lower())


SCHEMA = """
CREATE TABLE IF NOT EXISTS cases (
 id TEXT PRIMARY KEY, title TEXT NOT NULL, request TEXT NOT NULL,
 tenant_id TEXT NOT NULL, customer_id TEXT NOT NULL, order_id TEXT
);
CREATE TABLE IF NOT EXISTS orders (
 id TEXT NOT NULL, tenant_id TEXT NOT NULL, customer_id TEXT NOT NULL,
 amount_cents INTEGER NOT NULL CHECK(amount_cents > 0),
 refunded_cents INTEGER NOT NULL DEFAULT 0 CHECK(refunded_cents >= 0 AND refunded_cents <= amount_cents),
 status TEXT NOT NULL, created_at TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1,
 notes TEXT NOT NULL DEFAULT '', PRIMARY KEY(tenant_id, id)
);
CREATE TABLE IF NOT EXISTS policies (
 id TEXT NOT NULL, version INTEGER NOT NULL, tenant_id TEXT NOT NULL,
 active INTEGER NOT NULL CHECK(active IN (0,1)), kind TEXT NOT NULL,
 text TEXT NOT NULL, rules_json TEXT NOT NULL, PRIMARY KEY(id, version)
);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_policy_kind ON policies(tenant_id, kind) WHERE active=1;
CREATE TABLE IF NOT EXISTS runs (
 id TEXT PRIMARY KEY, case_id TEXT NOT NULL REFERENCES cases(id), mode TEXT NOT NULL,
 status TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 0,
 state_json TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
 id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL REFERENCES runs(id),
 kind TEXT NOT NULL, payload_json TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_run_id ON events(run_id,id);
CREATE TABLE IF NOT EXISTS run_sources (
 run_id TEXT NOT NULL REFERENCES runs(id), policy_id TEXT NOT NULL,
 policy_version INTEGER NOT NULL, fingerprint TEXT NOT NULL,
 PRIMARY KEY(run_id,policy_id,policy_version)
);
CREATE TABLE IF NOT EXISTS proposals (
 id TEXT PRIMARY KEY, run_id TEXT NOT NULL UNIQUE REFERENCES runs(id),
 tenant_id TEXT NOT NULL, customer_id TEXT NOT NULL, order_id TEXT NOT NULL,
 amount_cents INTEGER NOT NULL CHECK(amount_cents>0), reason TEXT NOT NULL,
 order_version INTEGER NOT NULL, policy_ids_json TEXT NOT NULL,
 policy_versions_json TEXT NOT NULL, policy_fingerprints_json TEXT NOT NULL,
 status TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS outbox (
 id TEXT PRIMARY KEY, proposal_id TEXT NOT NULL UNIQUE REFERENCES proposals(id),
 run_id TEXT NOT NULL REFERENCES runs(id), idempotency_key TEXT NOT NULL UNIQUE,
 payload_json TEXT NOT NULL, status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
 last_error TEXT, created_at TEXT NOT NULL, delivered_at TEXT
);
CREATE TABLE IF NOT EXISTS ledger (
 id TEXT PRIMARY KEY, proposal_id TEXT NOT NULL UNIQUE REFERENCES proposals(id),
 run_id TEXT NOT NULL REFERENCES runs(id), order_id TEXT NOT NULL,
 tenant_id TEXT NOT NULL, customer_id TEXT NOT NULL, amount_cents INTEGER NOT NULL CHECK(amount_cents>0),
 idempotency_key TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL
);
"""


class Store:
    def __init__(self, path):
        self.path = str(path)
        self._uri = self.path == ":memory:"
        self._dsn = f"file:caseflow-{uuid.uuid4().hex}?mode=memory&cache=shared" if self._uri else self.path
        self._keeper = None
        if not self._uri:
            Path(self.path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
        else:
            self._keeper = self._connect()
        with self._connection() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript("BEGIN IMMEDIATE;\n" + SCHEMA + "\nCOMMIT;")

    def close(self):
        if self._keeper is not None:
            self._keeper.close()
            self._keeper = None

    def _connect(self):
        conn = sqlite3.connect(self._dsn, timeout=10, isolation_level=None, uri=self._uri)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    @contextmanager
    def _connection(self):
        conn = self._connect()
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def _transaction(self):
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
                conn.commit()
            except BaseException:
                conn.rollback()
                raise

    @staticmethod
    def _decode(row):
        if row is None:
            return None
        value = dict(row)
        for key in list(value):
            if key.endswith("_json"):
                value[key[:-5]] = json.loads(value.pop(key))
        return value

    @staticmethod
    def _event(conn, run_id, kind, payload):
        conn.execute("INSERT INTO events(run_id,kind,payload_json,created_at) VALUES(?,?,?,?)",
                     (run_id, kind, _json(payload), _now()))

    @staticmethod
    def _require_run(conn, run_id):
        row = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            raise DomainError("run_not_found", "运行记录不存在。")
        return Store._decode(row)

    @staticmethod
    def _scope(conn, run_id):
        run = Store._require_run(conn, run_id)
        case = conn.execute("SELECT * FROM cases WHERE id=?", (run["case_id"],)).fetchone()
        return run, dict(case)

    def seed(self):
        """INSERT OR IGNORE preserves refunded orders and modified policy versions."""
        with self._transaction() as conn:
            for item in CASES:
                conn.execute("INSERT OR IGNORE INTO cases VALUES(:id,:title,:request,:tenant_id,:customer_id,:order_id)", item)
            for item in ORDERS:
                row = dict(item)
                row["created_at"] = (datetime.now(timezone.utc) - timedelta(days=row.pop("age_days"))).isoformat()
                conn.execute("""INSERT OR IGNORE INTO orders
                    (id,tenant_id,customer_id,amount_cents,refunded_cents,status,created_at,notes)
                    VALUES(:id,:tenant_id,:customer_id,:amount_cents,:refunded_cents,:status,:created_at,:notes)""", row)
            for item in POLICIES:
                # Never reactivate a fixture version when an operator has replaced it.
                if conn.execute("SELECT 1 FROM policies WHERE id=?", (item["id"],)).fetchone():
                    continue
                versions = [p for p in POLICIES if p["id"] == item["id"]]
                for policy in versions:
                    conn.execute("INSERT OR IGNORE INTO policies VALUES(?,?,?,?,?,?,?)",
                                 (policy["id"], policy["version"], policy["tenant_id"], int(policy["active"]),
                                  policy["kind"], policy["text"], _json(policy["rules"])))

    def list_cases(self):
        with self._connection() as conn:
            return [dict(row) for row in conn.execute("SELECT * FROM cases ORDER BY rowid")]

    def get_case(self, case_id):
        with self._connection() as conn:
            row = conn.execute("SELECT * FROM cases WHERE id=?", (case_id,)).fetchone()
            if row is None:
                raise DomainError("case_not_found", "案例不存在。")
            return dict(row)

    def create_run(self, case_id, mode, max_steps=12):
        if mode not in {"demo", "live"}:
            raise DomainError("invalid_mode", "运行模式必须为 demo 或 live。")
        if type(max_steps) is not int or not 1 <= max_steps <= 100:
            raise DomainError("invalid_budget", "工具预算必须是 1 到 100 之间的整数。")
        state = {"messages": [], "steps": 0, "tokens": 0, "usage_complete": True, "max_steps": max_steps}
        run_id = "run_" + uuid.uuid4().hex
        now = _now()
        with self._transaction() as conn:
            if conn.execute("SELECT 1 FROM cases WHERE id=?", (case_id,)).fetchone() is None:
                raise DomainError("case_not_found", "案例不存在。")
            conn.execute("INSERT INTO runs VALUES(?,?,?,?,?,?,?,?)",
                         (run_id, case_id, mode, "ready", 0, _json(state), now, now))
            self._event(conn, run_id, "run_created", {"mode": mode, "max_steps": max_steps})
            return self._require_run(conn, run_id)

    def get_run(self, run_id):
        with self._connection() as conn:
            return self._require_run(conn, run_id)

    def list_runs(self):
        with self._connection() as conn:
            return [self._decode(row) for row in conn.execute("SELECT * FROM runs ORDER BY created_at DESC,id")]

    def save_run(self, run_id, expected_revision, state, status):
        if type(expected_revision) is not int or not isinstance(state, dict):
            raise DomainError("invalid_run_state", "运行状态格式不正确。")
        with self._transaction() as conn:
            self._require_run(conn, run_id)
            result = conn.execute("""UPDATE runs SET state_json=?,status=?,revision=revision+1,updated_at=?
                WHERE id=? AND revision=?""", (_json(state), status, _now(), run_id, expected_revision))
            if result.rowcount != 1:
                raise Conflict("stale_revision", "运行状态已变化，请刷新后重试。")
            return self._require_run(conn, run_id)

    def add_event(self, run_id, kind, payload):
        with self._transaction() as conn:
            self._require_run(conn, run_id)
            self._event(conn, run_id, kind, payload)

    def events(self, run_id, after=0):
        with self._connection() as conn:
            self._require_run(conn, run_id)
            return [self._decode(row) for row in conn.execute(
                "SELECT id,kind,payload_json,created_at FROM events WHERE run_id=? AND id>? ORDER BY id",
                (run_id, after))]

    def recover(self):
        with self._transaction() as conn:
            # Queued jobs can still be ready when a process exits. Both queued
            # and executing jobs need explicit resume, never a paid startup call.
            rows = conn.execute("SELECT id,state_json FROM runs WHERE status IN ('ready','running')").fetchall()
            for row in rows:
                # Runtime owns detecting any persisted in-flight model reservation.
                conn.execute("UPDATE runs SET status='interrupted',revision=revision+1,updated_at=? WHERE id=?",
                             (_now(), row["id"]))
                self._event(conn, row["id"], "run_interrupted", {"reason": "service_restart"})
            return len(rows)

    @staticmethod
    def _order(conn, case, order_id):
        row = conn.execute("SELECT * FROM orders WHERE id=? AND tenant_id=? AND customer_id=?",
                           (order_id, case["tenant_id"], case["customer_id"])).fetchone()
        if row is None:
            # Same result for missing, foreign tenant, and foreign customer records.
            raise DomainError("order_not_found", "在当前客户的商户范围内找不到该订单，请转人工核实。")
        order = dict(row)
        created = datetime.fromisoformat(order["created_at"])
        if created.tzinfo is None:
            raise DomainError("invalid_order", "订单时间信息异常，请转人工处理。")
        order["age_days"] = (datetime.now(timezone.utc) - created).total_seconds() / 86400
        return order

    def get_order(self, run_id, order_id):
        with self._connection() as conn:
            _, case = self._scope(conn, run_id)
            return self._order(conn, case, order_id)

    @staticmethod
    def _fingerprint(policy):
        material = {k: policy[k] for k in ("id", "version", "tenant_id", "kind", "text", "rules")}
        return hashlib.sha256(_json(material).encode("utf-8")).hexdigest()

    def search_policy(self, run_id, query):
        with self._transaction() as conn:
            _, case = self._scope(conn, run_id)
            # Scope and active-version filtering happen BEFORE ranking.
            corpus = [self._decode(row) for row in conn.execute(
                "SELECT * FROM policies WHERE tenant_id=? AND active=1 ORDER BY id", (case["tenant_id"],))]
            terms = set(_tokens(query))
            documents = [_tokens(p["text"]) for p in corpus]
            avg_length = sum(map(len, documents)) / max(1, len(documents))
            results = []
            for policy, tokens in zip(corpus, documents):
                score = 0.0
                for term in terms:
                    frequency = tokens.count(term)
                    if not frequency:
                        continue
                    df = sum(term in document for document in documents)
                    inverse = math.log(1 + (len(documents) - df + 0.5) / (df + 0.5))
                    score += inverse * frequency * 2.2 / (frequency + 1.2 * (0.25 + 0.75 * len(tokens) / max(1, avg_length)))
                if score > 0:
                    policy["score"] = round(score, 6)
                    results.append(policy)
            results.sort(key=lambda p: (-p["score"], p["id"]))
            results = results[:5]
            for policy in results:
                conn.execute("""INSERT INTO run_sources VALUES(?,?,?,?)
                    ON CONFLICT(run_id,policy_id,policy_version) DO UPDATE SET fingerprint=excluded.fingerprint""",
                             (run_id, policy["id"], policy["version"], self._fingerprint(policy)))
            self._event(conn, run_id, "policy_retrieved", {
                "sources": [{"id": p["id"], "version": p["version"], "score": p["score"]} for p in results]})
            return results

    @staticmethod
    def _citations(conn, run_id, case, policy_ids):
        if (not isinstance(policy_ids, list) or not policy_ids or len(policy_ids) > 5
                or any(not isinstance(p, str) or not p or len(p) > 100 for p in policy_ids)
                or len(set(policy_ids)) != len(policy_ids)):
            raise DomainError("invalid_citation", "必须提供不重复的、已在本次运行中检索到的有效政策来源。")
        policies = []
        for policy_id in policy_ids:
            row = conn.execute("""SELECT p.* FROM policies p JOIN run_sources s
                ON s.policy_id=p.id AND s.policy_version=p.version
                WHERE s.run_id=? AND p.id=? AND p.tenant_id=? AND p.active=1""",
                               (run_id, policy_id, case["tenant_id"])).fetchone()
            if row is None:
                raise DomainError("invalid_citation", "政策来源未在本次运行中检索、已失效或不属于当前商户。")
            policy = Store._decode(row)
            retrieved = conn.execute("SELECT fingerprint FROM run_sources WHERE run_id=? AND policy_id=? AND policy_version=?",
                                     (run_id, policy_id, policy["version"])).fetchone()["fingerprint"]
            if retrieved != Store._fingerprint(policy):
                raise DomainError("stale_policy", "政策已变化，请重新检索。")
            policies.append(policy)
        return policies

    def validate_citations(self, run_id, policy_ids):
        with self._connection() as conn:
            _, case = self._scope(conn, run_id)
            policies = self._citations(conn, run_id, case, policy_ids)
            return [{"id": p["id"], "version": p["version"]} for p in policies]

    @staticmethod
    def _eligible(order, policies, amount_cents):
        if type(amount_cents) is not int or amount_cents <= 0:
            raise DomainError("invalid_amount", "退款金额必须为正整数分。")
        governing = [p for p in policies if p["kind"] == "refund"]
        if len(governing) != 1:
            raise DomainError("invalid_citation", "必须引用当前商户有效的退款政策。")
        rules = governing[0]["rules"]
        window, cap, statuses = rules.get("refund_window_days"), rules.get("max_refund_cents"), rules.get("allowed_statuses")
        if (type(window) is not int or window < 0 or type(cap) is not int or cap <= 0
                or not isinstance(statuses, list) or not statuses or rules.get("full_remaining_only") is not True):
            raise DomainError("invalid_policy", "退款政策配置不完整，请转人工处理。")
        remaining = order["amount_cents"] - order["refunded_cents"]
        if order["status"] not in statuses or remaining <= 0:
            raise DomainError("refund_ineligible", "订单状态不支持自动退款申请，请转人工处理。")
        if order["age_days"] < 0 or order["age_days"] > window:
            raise DomainError("refund_ineligible", "订单已超出退款政策的时间窗口，请转人工处理。")
        if amount_cents != remaining or amount_cents > cap:
            raise DomainError("invalid_amount", "申请金额必须等于全部剩余可退金额，且不能超过商户上限。")

    def create_proposal(self, run_id, order_id, amount_cents, reason, policy_ids):
        with self._transaction() as conn:
            run, case = self._scope(conn, run_id)
            existing = conn.execute("SELECT * FROM proposals WHERE run_id=?", (run_id,)).fetchone()
            if existing is not None:
                existing = self._decode(existing)
                if (existing["order_id"] != order_id or existing["amount_cents"] != amount_cents
                        or existing["reason"] != reason or existing["policy_ids"] != policy_ids):
                    raise Conflict("proposal_exists", "本次运行已生成另一份退款申请。")
                if existing["status"] != "proposed":
                    raise Conflict("proposal_decided", "退款申请已经审批，不能重新提交。")
                return existing
            if run["status"] not in {"ready", "running", "interrupted"}:
                raise Conflict("run_terminal", "本次运行已结束，不能再创建申请。")
            if case["order_id"] is None or case["order_id"] != order_id:
                raise DomainError("order_not_found", "申请订单必须与本次客户请求的订单一致。")
            order = self._order(conn, case, order_id)
            policies = self._citations(conn, run_id, case, policy_ids)
            self._eligible(order, policies, amount_cents)
            proposal_id = "prop_" + uuid.uuid4().hex
            versions = [{"id": p["id"], "version": p["version"]} for p in policies]
            fingerprints = {p["id"]: self._fingerprint(p) for p in policies}
            conn.execute("INSERT INTO proposals VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         (proposal_id, run_id, case["tenant_id"], case["customer_id"], order_id, amount_cents,
                          reason, order["version"], _json(policy_ids), _json(versions), _json(fingerprints), "proposed", _now()))
            self._event(conn, run_id, "refund_proposed", {"proposal_id": proposal_id, "amount_cents": amount_cents,
                                                          "order_version": order["version"], "policy_versions": versions})
            return self._decode(conn.execute("SELECT * FROM proposals WHERE id=?", (proposal_id,)).fetchone())

    @staticmethod
    def _revalidate(conn, proposal):
        _, case = Store._scope(conn, proposal["run_id"])
        if (proposal["tenant_id"] != case["tenant_id"] or proposal["customer_id"] != case["customer_id"]
                or proposal["order_id"] != case["order_id"]):
            raise DomainError("stale_scope", "客户请求范围已变化，原申请不能执行。")
        order = Store._order(conn, case, proposal["order_id"])
        if order["version"] != proposal["order_version"]:
            raise DomainError("stale_order", "订单版本已变化，请重新核实后创建申请。")
        policies = Store._citations(conn, proposal["run_id"], case, proposal["policy_ids"])
        if ([{"id": p["id"], "version": p["version"]} for p in policies] != proposal["policy_versions"]
                or {p["id"]: Store._fingerprint(p) for p in policies} != proposal["policy_fingerprints"]):
            raise DomainError("stale_policy", "退款政策已变化，原申请不能执行。")
        Store._eligible(order, policies, proposal["amount_cents"])
        return order

    def approve(self, proposal_id, approved: bool):
        if type(approved) is not bool:
            raise DomainError("invalid_decision", "审批决定必须是布尔值。")
        with self._transaction() as conn:
            row = conn.execute("SELECT * FROM proposals WHERE id=?", (proposal_id,)).fetchone()
            if row is None:
                raise DomainError("proposal_not_found", "退款申请不存在。")
            proposal = self._decode(row)
            if proposal["status"] != "proposed":
                if (approved and proposal["status"] in {"approved", "refunded"}) or (not approved and proposal["status"] == "rejected"):
                    return proposal
                raise Conflict("proposal_decided", "退款申请已处理，不能更改审批决定。")
            run = self._require_run(conn, proposal["run_id"])
            if run["status"] != "awaiting_approval":
                raise Conflict("approval_not_ready", "运行尚未进入待审批状态，请刷新后重试。")
            if approved:
                self._revalidate(conn, proposal)
                payload = {k: proposal[k] for k in ("id", "run_id", "tenant_id", "customer_id", "order_id", "amount_cents", "order_version", "policy_versions")}
                conn.execute("""INSERT INTO outbox(id,proposal_id,run_id,idempotency_key,payload_json,status,created_at)
                    VALUES(?,?,?,?,?,'pending',?)""",
                             ("out_" + uuid.uuid4().hex, proposal_id, proposal["run_id"], "refund:" + proposal_id, _json(payload), _now()))
            proposal_status, run_status = ("approved", "queued") if approved else ("rejected", "rejected")
            conn.execute("UPDATE proposals SET status=? WHERE id=? AND status='proposed'", (proposal_status, proposal_id))
            conn.execute("UPDATE runs SET status=?,revision=revision+1,updated_at=? WHERE id=?",
                         (run_status, _now(), proposal["run_id"]))
            self._event(conn, proposal["run_id"], "approval_decision", {"proposal_id": proposal_id, "approved": approved})
            return self._decode(conn.execute("SELECT * FROM proposals WHERE id=?", (proposal_id,)).fetchone())

    def dispatch(self, fail_after_effect=False):
        """Two transactions model receiver commit followed by transport ACK.

        A real provider must implement the same idempotency-key contract. This local
        receiver demonstrates exactly-once business effects over repeated delivery;
        it does not claim a distributed exactly-once transport.
        """
        with self._transaction() as conn:
            row = conn.execute("SELECT * FROM outbox WHERE status='pending' ORDER BY created_at,id LIMIT 1").fetchone()
            if row is None:
                return None
            item = self._decode(row)
            conn.execute("UPDATE outbox SET attempts=attempts+1,last_error=NULL WHERE id=?", (item["id"],))
            existing = conn.execute("SELECT * FROM ledger WHERE idempotency_key=?", (item["idempotency_key"],)).fetchone()
            if existing is None:
                proposal = self._decode(conn.execute("SELECT * FROM proposals WHERE id=?", (item["proposal_id"],)).fetchone())
                try:
                    if proposal["status"] != "approved":
                        raise DomainError("approval_required", "只有已审批的退款申请可以执行。")
                    expected = {k: proposal[k] for k in ("id", "run_id", "tenant_id", "customer_id", "order_id", "amount_cents", "order_version", "policy_versions")}
                    if item["payload"] != expected or item["idempotency_key"] != "refund:" + proposal["id"]:
                        raise DomainError("invalid_delivery", "投递内容与已审批申请不一致。")
                    self._revalidate(conn, proposal)
                except DomainError as exc:
                    conn.execute("UPDATE outbox SET status='failed',last_error=? WHERE id=?", (exc.code, item["id"]))
                    conn.execute("UPDATE proposals SET status='invalidated' WHERE id=?", (proposal["id"],))
                    conn.execute("UPDATE runs SET status='error',revision=revision+1,updated_at=? WHERE id=?", (_now(), item["run_id"]))
                    self._event(conn, item["run_id"], "delivery_rejected", {"error": exc.code, "detail": exc.detail})
                    return self._decode(conn.execute("SELECT * FROM outbox WHERE id=?", (item["id"],)).fetchone())
                conn.execute("INSERT INTO ledger VALUES(?,?,?,?,?,?,?,?,?)",
                             ("refund_" + uuid.uuid4().hex, proposal["id"], item["run_id"], proposal["order_id"],
                              proposal["tenant_id"], proposal["customer_id"], proposal["amount_cents"], item["idempotency_key"], _now()))
                updated = conn.execute("""UPDATE orders SET refunded_cents=refunded_cents+?,status='refunded',version=version+1
                    WHERE tenant_id=? AND customer_id=? AND id=? AND version=? AND refunded_cents+?<=amount_cents""",
                                       (proposal["amount_cents"], proposal["tenant_id"], proposal["customer_id"], proposal["order_id"],
                                        proposal["order_version"], proposal["amount_cents"]))
                if updated.rowcount != 1:
                    raise Conflict("stale_order", "订单状态已变化，退款未执行。")
                self._event(conn, item["run_id"], "refund_effect_committed", {"proposal_id": proposal["id"],
                    "idempotency_key": item["idempotency_key"], "amount_cents": proposal["amount_cents"], "simulated": True})
            else:
                self._event(conn, item["run_id"], "refund_delivery_deduplicated", {"idempotency_key": item["idempotency_key"]})

        # Intentional crash point: payment committed, outbox still pending.
        if fail_after_effect:
            self.add_event(item["run_id"], "simulated_ack_loss", {"outbox_id": item["id"], "retry_safe": True})
            raise DomainError("simulated_ack_loss")

        with self._transaction() as conn:
            current = conn.execute("SELECT status FROM outbox WHERE id=?", (item["id"],)).fetchone()
            if current["status"] == "pending":
                conn.execute("UPDATE outbox SET status='delivered',delivered_at=?,last_error=NULL WHERE id=?", (_now(), item["id"]))
                conn.execute("UPDATE proposals SET status='refunded' WHERE id=?", (item["proposal_id"],))
                conn.execute("UPDATE runs SET status='completed',revision=revision+1,updated_at=? WHERE id=?", (_now(), item["run_id"]))
                self._event(conn, item["run_id"], "refund_delivery_acknowledged", {"outbox_id": item["id"], "simulated": True})
            return self._decode(conn.execute("SELECT * FROM outbox WHERE id=?", (item["id"],)).fetchone())

    def proposals(self, run_id=None):
        with self._connection() as conn:
            if run_id is None:
                rows = conn.execute("SELECT * FROM proposals ORDER BY created_at DESC,id")
            else:
                self._require_run(conn, run_id)
                rows = conn.execute("SELECT * FROM proposals WHERE run_id=? ORDER BY created_at DESC,id", (run_id,))
            return [self._decode(row) for row in rows]

    def outbox(self):
        with self._connection() as conn:
            return [self._decode(row) for row in conn.execute("SELECT * FROM outbox ORDER BY created_at DESC,id")]

    def ledger(self):
        with self._connection() as conn:
            return [dict(row) for row in conn.execute("SELECT * FROM ledger ORDER BY created_at DESC,id")]

    def snapshot(self):
        with self._connection() as conn:
            return {"runs": conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0],
                    "awaiting_approval": conn.execute("SELECT COUNT(*) FROM runs WHERE status='awaiting_approval'").fetchone()[0],
                    "pending_deliveries": conn.execute("SELECT COUNT(*) FROM outbox WHERE status='pending'").fetchone()[0],
                    "refunds": conn.execute("SELECT COUNT(*) FROM ledger").fetchone()[0],
                    "refunded_cents": conn.execute("SELECT COALESCE(SUM(amount_cents),0) FROM ledger").fetchone()[0]}
