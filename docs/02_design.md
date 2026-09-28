# Template Design Specification — LOG-C2-109 Customs Shipment Document Completeness Validator

> Pattern: DocGenerationAgent（Cat 2 蓄積型）

## Position in AgentCore Architecture

- **Agent Class**: `CustomsDocValidatorGraph`（`src/graph/graph.py`）
- **L1 Base**: **AgentBaseGraph**（L1 直継承。`AutonomousBaseGraph` ではない — 固定パイプラインであり
  agent 自身が次アクションを決める think→act ループではないため）
- **Composition**: 外側 `AgentBaseGraph` の `main` スロットに `GraphNode`（`CustomsWorkflowGraphNode`）を割当て、
  内側の domain workflow graph（`src/graph/domain_workflow_graph.py`）に多段ロジックを閉じ込める（Cat 2 標準構成）
- **Three-Layer Separation**:
  - State: flat TypedDict composition（primitives + JSON-serializable のみ・Pydantic 不可 = msgpack 非互換）
  - Node: L1 inheritance（Template Method: `execute(self, state) -> dict` の override のみ。部分 dict 返却）
  - Graph: composition（`register_nodes()` でノード置換、内側は BaseGraph の custom topology）

## Architecture Overview

本テンプレは 2 層構成:

- **外側グラフ**（`graph.py`・`AgentBaseGraph`）: 固定 5 ノード backbone。`add_edges()` は override しない。
- **内側グラフ**（`domain_workflow_graph.py`・`BaseGraph` 直継承）: Stage 0–5 の custom topology。
  `CustomsWorkflowGraphNode.get_subgraph()` から instantiate される。

### Outer Node Configuration（固定 backbone）

| Node | Responsibility | Input State | Output State | Inherits/Overrides |
|------|---------------|-------------|--------------|-------------------|
| initialize | ctx 確立・schema_version 付与 | user_input | 初期 AgentState | InitializeNode (default) |
| pre_process | 入力パケット JSON 検証・案件キー抽出・正規化。パケットの `customer_id` を認可済み顧客（`caller_id` 由来）と**照合**（不一致は fail-closed。接続選択には使わない） | user_input | validated_input, customer_id | `PreProcessNode`（FunctionNode） |
| main | 内側 domain workflow graph を invoke し結果をマージ | validated_input | primary_check_result 等 | `CustomsWorkflowGraphNode`（GraphNode） |
| post_process | 出力 contract 整形（5 区分）・S-3 専門職境界の最終検証・監査 emit | primary_check_result | formatted_output | `PostProcessNode`（FunctionNode） |
| finalize | 出力確定 | formatted_output | final result | FinalizeNode (default) |

### AGENTIC STAR Marketplace entry adapter

リポジトリ直下の `cli.py` は `CustomsDocValidatorGraph` を継承する `MarketplaceGraph` を公開する。
Marketplace runner が渡す `conversation_history` は S-2 の走査対象外の自由文なので、受け取った
`input_context` はマージせず破棄し、`STG_DEFAULT_CUSTOMER_ID` から得た `customer_id` だけで再構築する。
同時に `STG_DEFAULT_CALLER_ID → STG_DEFAULT_CUSTOMER_ID` の単一顧客 binding を compile 前の config に設定し、
入力 JSON 内の `customer_id` はこのデプロイ値に置換する。両 ID と Azure 接続 3 値は、未設定・空・空白を含む
場合に constructor / `provision_secrets()` で fail closed となる。

Marketplace は read-only であり、`invoke` 以外の mode を安全な不正 mode に変換して既存 S-2 で拒否する。
顧客専用 KB が config に明示注入されていない場合は、検索空振りだけを返し書込 API を拒否する専用接続を使う。
shared/default KB へはフォールバックしない。`hitl.enabled: true` は維持するが Marketplace runner は resume できないため、
frozen `InvocationContext` を `dataclasses.replace(..., hitl_allowed=False)` で複製して中断だけを抑止する。

Azure LLM wrapper は compile 前に注入するが接続値を保持しない。`AzureOpenAIClient` は LLM メソッド内だけで import し、
各 `complete()` / `stream()` ごとに bound `SecretProvider` から接続値を解決して新しい client を生成する。

### Inner Domain Workflow（`domain_workflow_graph.py`・BaseGraph custom topology）

Stage 0–5 を内側ノードとして実装。`register_nodes()` は super() を呼ばない（BaseGraph は abstract）。
initialize/finalize は内側に登録しない（外側 backbone の責務）。

| Stage | Inner Node | 主担当 | 処理 | ガード |
|---|---|---|---|---|
| 0 | `intake_node` | deterministic | パケット受領・テナント/荷主/案件/書類種別/権限確定 | 案件ID・権限・原本不明 → `requires_human_review`。外部送信しない |
| 1 | `extract_validate_node` | deterministic | OCR/構造化抽出結果の検証・SKU/数量/価格/原産地/取引条件/品名の正規化 | 抽出信頼度不足・読取不能・単位不明 → 人間確認。LLM が数値補完しない |
| 2 | `rule_match_node` | deterministic | 承認済み pre-review packet checklist（DB）から必要書類・説明項目を照合 | DB 入力は owner/version/適用期間/source 必須。HS/許認可の推論結論を含めない |
| 3 | `consistency_node` | **LLM** | 書類間の自由記述と構造化項目を比較し同一貨物・取引としての整合を当てはめ評価 | claim ごとに原本 source ID を付与 |
| 4 | `recall_node` | retrieval | 差し戻し理由・照会履歴 KB から類似不備を意味検索 | 類似事例は確認観点のみ。過去 HS/許認可/申告結論を継承しない |
| 5 | `assemble_node` | **LLM + HITL** | 一次チェック結果を生成し 5 区分に分離。曖昧なら `unknowns`+`broker_questions` に止める | `hs_classification`/`license_final_decision`/`customs_declaration` を schema から排除。fail-closed で通関士レビューへ HITL |

内側 topology（linear + Stage5 の HITL 分岐）:

```
START → intake → extract_validate → rule_match → consistency → recall → assemble → END
                        ↓ (低信頼/権限不明で fail-closed)
                   requires_human_review 経由で assemble へ（unknowns として集約）
```

### Data Flow（外側 backbone）

```
START → initialize → pre_process → main → {route} → post_process → finalize → END
                                            ↓ (retry, max 3)
                                          pre_process
```

> **この backbone・`{route}`・retry edge は全て `AgentBaseGraph` が既に配線済みの継承挙動**である。
> テンプレは `add_edges()`/`route()` を override しない。`route()` は main の後に `state["status"]` を読み、
> `success`→post_process / `retry`→pre_process（max_retry まで）/ その他→finalize へ分岐する framework 標準の
> 挙動をそのまま使う（本テンプレ独自の retry ループは実装しない）。

### Output Contract（5 区分）

`post_process` が整形する出力 contract:

| 区分キー | 内容 | 禁止 |
|---|---|---|
| `document_completeness` | 承認済み checklist に対する必要証憑・説明項目の有無 | HS 確定前提の必要書類判定 |
| `consistency_findings` | 品名/数量/価格/原産地/取引条件の整合・不整合 + source ID | 根拠のない矛盾認定 |
| `unknowns` | OCR 不確実性・単位不明・SKU 未一致・説明資料不足・適用ルール不明 | 不明点を適合/申告可能と扱う |
| `similar_return_reasons` | 過去差し戻し・照会から得た確認観点 | 過去 HS/許認可/申告結論のコピー |
| `broker_questions` | 通関士に確認する論点・不足資料 | HS 確定・許認可最終判断・申告指図 |

### State Definition（flat TypedDict — `src/schemas/state.py`）

`AgentState` を継承し、以下のドメインフィールドを追加。全て primitive / JSON-serializable。

> **継承済みで再宣言しない framework フィールド**（実機 `AgentState` で確認済み。追加宣言不要）:
> `user_input`, `validated_input`, `status`, `caller_trust_level`, `correlation_id`, `session_id`,
> `thread_id`, `node_history`, `error_log`, `formatted_output`, `result`,
> および HITL 7 フィールド（`hitl_feedback` / `hitl_metadata` / `hitl_status` / `hitl_draft` /
> `hitl_count` / `hitl_allowed` / `subgraph_thread_id`）。
> `validated_input`（pre_process の出力）と `hitl_draft`（resume 冪等性）は既に継承済みのため下表に含めない。

| Field | Type | Purpose | Required |
|-------|------|---------|----------|
| `customer_id` | `str` | 顧客テナント識別子（どの顧客専用 DB/KB へ接続するかを確定。レコード内フィルタキーではなく接続コンテキスト） | ✅ |
| `shipment_case_id` | `str` | 出荷案件 ID | ✅ |
| `document_packet` | `list[dict]` | 書類種別・抽出項目・信頼度の構造化リスト | ✅ |
| `sku_refs` | `list[dict]` | 抽出された SKU・数量・価格・原産地・取引条件 | — |
| `checklist_matches` | `list[dict]` | Stage2 の DB 照合結果（owner/version/source 付き） | — |
| `document_completeness` | `dict` | 出力区分① | — |
| `consistency_findings` | `list[dict]` | 出力区分② | — |
| `unknowns` | `list[dict]` | 出力区分③（fail-closed 集約先） | — |
| `similar_return_reasons` | `list[dict]` | 出力区分④ | — |
| `broker_questions` | `list[dict]` | 出力区分⑤ | — |
| `requires_human_review` | `bool` | fail-closed フラグ（HITL トリガ） | — |
| `primary_check_result` | `dict` | 内側 graph の集約結果（main→post_process 受け渡し） | — |

**State Constraints (mandatory):**
- Flat TypedDict only（primitives + JSON-serializable）。ネスト dict/list は許容だが中身も primitive のみ
- No JWT / API keys / credentials in State（checkpoint DB 漏洩防止）
- InvocationContext は State に格納しない（`InvocationContext.from_state(state)` で取得）
- No Pydantic / dataclass / arbitrary Python objects（msgpack 非互換）
- インボイス・価格・取引先等の機微値は最小入力・許可フィールドに限定（allowlist）。ログは参照 ID 化

## Framework Utilization

### Shared Components Used
- [x] InvocationContext（correlation_id, session_id, caller_trust_level, hitl_allowed）— `from_state()` で取得
- [x] S-2: `_extra_security_gate_input()` — `pre_process` で入力パケットのテナント/案件キー整合・
      許可フィールド外の混入チェック。default PII scan に追加のドメインチェック
- [x] S-3: `_extra_security_gate_output()` — `post_process` で**専門職境界の構造的排除を検証**（下記 S-3 設計参照）。
      preservation/filtering の両立: 出力・payload に HS 分類/許認可判断/申告可否/申告 payload/外部送信能力が
      存在しないことを検証（filtering）し、根拠 source ID の欠落を検出（preservation）
- [x] S-4: `emit_trace_event(event_type, payload, state)` — 各 `execute()` 内で最低 1 回のドメインイベント
      （副作用の有無に関わらず必須。`node_start`/`node_complete`/`node_error` は framework 自動 emit のため重複禁止）

> **S-2/S-3 gate behaviour by node type:**
> - `FunctionNode` subclass（pre_process / post_process / 内側の deterministic ノード）→ framework `@final` gate が
>   自動実行。`_extra_security_gate_input/output()` のみ拡張（`_security_gate_*` 直定義は import 時 TypeError）
> - `GraphNode`（main の `CustomsWorkflowGraphNode`）→ S-2/S-3 は no-op passthrough（内側ノードの gate が適用済み）
> - 内側 BaseGraph の LLM/retrieval ノードは FunctionNode を継承し `@final` gate を自動享受

### Data Governance（蓄積型データ統治・A-4 判断）

蓄積型データは DB と KB の 2 層に分離:

- **DB**（SKU マスタ・品目→必要証憑ルール = 承認済み pre-review packet checklist）: owner/version/適用期間/source 必須。
  HS 分類・許認可要否の推論結論を含めない。
- **KB**（通関士差し戻し理由・照会履歴の意味検索）: 人間確認済みの差し戻し理由のみ登録。
  過去の HS/許認可/申告結論は登録しない。類似事例は確認観点の提示に限る。

**テナント分離は KB 接続レベルで担保（`scoped-retrieval` は不採用）**:

- 顧客テナント（フォワーダー/通関業者/輸出入企業）ごとに差し戻し理由 KB は**物理的に分離**される
  （顧客専用 KB。どの顧客の KB へ接続するかは呼び出しコンテキストで確定）。テナント間の混在は
  接続レベルで構造的に起こり得ないため、**レコード内にテナントキーを持たせて検索時にフィルタする必要はない**。
- Stage4 の KB 検索は、確定した顧客専用 KB に対して意味検索するだけで閉じる。
- ⚠️ **`scoped-retrieval` 部品は採用しない**。同部品の scope_keys は「マルチテナント分離ではなく
  1 顧客の KB **内**で混ぜてはいけない区画を絞る行キー」であり（USAGE 冒頭警告）、**顧客内にそうした区画が
  無いドメインには入れない**と明記されている。本 AG は 1 顧客の差し戻し履歴を荷主・案件をまたいで横断想起する
  方が確認観点が充実する＝顧客内に「混ぜてはいけない区画」が無いため、この部品の適用対象外。
**顧客 → KB 接続の選択契約（セキュリティ核・MR-B 必須）**:

- 接続先 KB は **Gateway 認証済み `caller_id` → 認可済み顧客 → 事前 provision 済み KB 接続** の経路でのみ確定する。
  `caller_id` は `InvocationContext` から取得（State の caller_trust_level / caller_id 経由）。
- **入力パケット由来の `customer_id` で接続先 KB を選んではならない**（他社 KB への横断接続を防ぐ）。
  パケットの `customer_id` は認可済み顧客との**照合のみ**に使い、不一致・不明・未マッピングは **fail-closed**
  （default/fallback KB を持たない）。
- pre_process はパケットから `customer_id` を抽出しつつ、この照合を通過した場合のみ Stage4 の接続に進む。

**Stage4 自前 retrieval の結果セマンティクス（MR-B 必須）**:

- Stage4 は自前 retrieval ノードで実装（顧客専用 KB backend への意味検索）。`scoped-retrieval` の**安全契約を写経踏襲**:
  - **record 契約**: `retrieved_records` は `id` / `source_id` 必須・`source_tier` + allowlist で境界した `metadata` 付き。
    backend の生 record を素通しさせない（safe projection = allowlist）。
  - normalize_query は書込側と同一関数を共有（未配線で本番検索しない）。metadata 文字列は形状ガード（≤64 字・改行なし）。
- **結果の区別**（fail-open 防止）:
  - **空振り / ID 不足 hit** → 空の safe result + `degradation_reason`（`status=success`）→ 最終的に `unknowns` / broker review へ。
  - **KB 解決失敗 / backend エラー** → **fail-closed**（`status=error`）。システムエラーと空振りを混同しない。
- scope-partition 機構（scope_keys / scope 検証）は使わない（テナント分離は接続レベルで担保済み）。

### S-3 専門職境界の構造的排除（本テンプレの安全核）

`post_process._extra_security_gate_output()` で以下を**出力全体（ネスト含む）に対して検証**し、
検出時は `SecurityViolationError`（fail-closed）:

- 出力キー / payload に `hs_classification`・`license_final_decision`・`customs_declaration` が存在しないこと
- `similar_return_reasons`（過去 return record 経由）に HS/許認可/申告の確定結論がコピーされていないこと
- 外部送信を示す payload（申告先 URL・NACCS 接続情報等）が含まれないこと
- `consistency_findings` の各 claim に原本 source ID が付いていること（preservation）

これは execute が書き込んだ state に依存せず、**出力 dict を直接走査**して判定する。
判定はキーの allowlist 方式（業務キーは許可リストで管理・denylist に頼らない）。

### Composition Pattern

- **Pattern**: GraphNode（内側 BaseGraph subgraph）
- **Composition target**: `src/graph/domain_workflow_graph.py`（`CustomsWorkflowGraph`）
- **Error propagation strategy**: `propagate`（内側エラーは `SubgraphError` として fail-fast。graceful degradation より
  fail-closed を優先する安全ドメインのため）
- **propagate_hitl**: `True`（内側 Stage5 の通関士レビュー interrupt を外側呼び出し元へ propagate する。
  外側 config も `hitl.enabled: true` が必要）
- **GraphNode 抽象メソッド実装**: `get_subgraph()` / `extract_input()` / `merge_output()`（全て必須）

### HITL 設計（`hitl.enabled: true`）

- Stage5 `assemble_node`（内側 FunctionNode ではなく BaseNode 継承）で D6 パターンの `interrupt()` を使用
- **`interrupt()` は必ず `state.get("hitl_allowed", True)` でガード**（ガードの無い `interrupt()` は
  自動化パイプラインからの呼び出しでデッドロックする）。
  `hitl_allowed` は Gateway / 親 GraphNode が設定する **invocation/state ポリシー**（テンプレは書き込まない）。
  - `hitl_allowed=True`（既定・人間が介在する通常呼び出し）: interrupt して通関士レビューへ。
    `propagate_hitl=True` により内側 Stage5 の interrupt が外側呼び出し元へ伝播する
  - `hitl_allowed=False`（自動パイプライン・親エージェントからの合成呼び出し）: interrupt をスキップし
    `requires_human_review=True` を出力に残して先へ進む（fail-closed の記録は残す）
  - `propagate_hitl`（=内側 interrupt を外へ出すか）と `hitl_allowed`（=そもそも interrupt するか）は別概念
- `hitl_draft` で resume 時の LLM 再実行を防止（framework 継承フィールド）
- `HitlStatus` の 3 分岐（APPROVED / CORRECTED / REJECTED）を全て handle
- `config.yaml`: `memory_enabled: true`（checkpointer 必須）+ `hitl.enabled: true` + `hitl.max_hitl: 8`

## Import Isolation Confirmation
- [x] Template は agenticstar-platform SDK（Level 0）を import しない
- [x] Import 先: `framework/` と `shared/` のみ（`agents/base/` 不要）

## Design Decision Record

| Decision | Option A | Option B | Chosen | Rationale |
|----------|----------|----------|--------|-----------|
| L1 base type | AgentBaseGraph | AutonomousBaseGraph | **AgentBaseGraph** | 固定パイプライン。agent 自身が次アクションを決めない（think→act ループなし） |
| Composition pattern | Standalone（main=FunctionNode） | GraphNode（inner subgraph） | **GraphNode** | Stage 0–5 の多段ドメインロジックを内側 graph に閉じ込め、外側 backbone を汚さない（Cat 2 標準） |
| Inner graph base | BaseGraph（custom topology） | AgentBaseGraph（5 slots） | **BaseGraph** | Stage 0–5 は fixed 5-slot ではない custom node 名・topology のため |
| error_strategy | handle（graceful） | propagate（fail-fast） | **propagate** | 安全ドメイン。曖昧・エラーは握り潰さず fail-closed で人間へ |
| S-3 実装 | filtering のみ | filtering + preservation | **両立** | 専門職境界排除（filtering）+ 根拠 source ID 保全（preservation） |

## 適用部品一覧（parts — MR-B の写経指示書）

MR-B はゼロから書かず、`parts/` の実装部品を写経して adaptation_points を本テンプレ仕様で埋める。
適用時には各部品の version/status を再確認する。**実際に適用されている版は、写経先ファイル冒頭の
`# PART:` マーカーが正**（部品の版上げは挙動を伴うため別途行う）。

| part-id | 適用版 | 適用先 | adaptation_points の埋め方 |
|---|---|---|---|
| `cat2-graph-skeleton` | `# PART:` 参照 | `main` スロット（`CustomsWorkflowGraphNode`） | `subgraph_factory=`→内側 `CustomsWorkflowGraph` を返す factory。`_parent_config`→llm/db_client/kb_client/top_k を inner へ。`mode_specs`→single mode（sub_result→outer の 5 出力区分 field map）。`error_strategy="propagate"`。`propagate_hitl=True`（HITL 有） |
| `evidence-gate` | `# PART:` 参照 | `post_process` の S-3（`consistency_findings` の source ID 実在検証） | `gate_kind`→"customs_primary_check"。`cited_ids_key`/`claim_source_keys`→各 claim の原本 source ID キー。`retrieved_records_key`→Stage1/2 の実書類レコード。捏造/不一致 ID を fail-closed。`passthrough_keys`→5 出力区分（node_history/error_log は含めない） |
| `output-envelope` | `# PART:` 参照 | `post_process`（5 出力区分の formatted_output 整形） | `format_invoke`→5 区分（document_completeness/consistency_findings/unknowns/similar_return_reasons/broker_questions）。`required_fields_by_mode`→fail-open 防止の必須キー。`_sanitize_client_echoes`→専門職境界排除の出力再スキャン（HS/許認可/申告キー・外部送信 payload を遮断） |

> **`scoped-retrieval` は不採用**（§Data Governance 参照）。同部品の scope_keys は「1 顧客 KB 内の
> 混ぜてはいけない区画」を絞る機構で、顧客内に区画が無い本 AG は適用対象外。Stage4 の KB 検索は
> 顧客専用 KB への意味検索を自前実装し、**安全契約のみ写経踏襲**（safe projection allowlist・normalize_query 共有・
> source_id 必須・空振り時 degradation_reason）。scope-partition 機構（scope_keys/scope 検証）は使わない。
>
> **KB service base / identifier-sanitizer / graph-node-contract-tests / audit-event-helper** は
> registry の second_wave（PART.yaml 未整備）。MR-B 時点で ready 化されていれば適用、未整備なら
> 該当ロジック（KB 接続/ID 生成/safe projection・サニタイザ多層・GraphNode 契約テスト mixin・emit ヘルパー）を
> 上記部品の写経に合わせて自前実装する（部品と同じ契約に揃える）。
>
> **not_parts（部品化しない・設計テンプレ扱い）**: Stage0-2 の deterministic ノード（intake/extract_validate/
> rule_match）・Stage4 の自前 retrieval・Stage3/5 の LLM prompt 本文・出力 schema はドメイン固有のため部品化せず
> 本テンプレで実装（LLM 呼出/parse/fallback/evidence hook のみ `llm-draft-stage` 相当の型に揃える）。

## SDK 実装契約メモ（MR-B 実装時の必読・スパイクで実機確認済み）

1. **`GraphNode._parent_config()` は wheel（agenticstar-agentcore==1.0.0）に存在しない** —
   `src/examples/graph_cat2_sample.py` は `self._parent_config()` を呼ぶが**実機の抽象は 3 メソッドのみ
   （get_subgraph / extract_input / merge_output）で `_parent_config` は無い**ため、サンプルの写経は禁止。
   → **本テンプレは `parts:cat2-graph-skeleton` を写経する**。この部品は `_parent_config` を部品側で自前
   定義済み（inner graph へ llm/service/store/top_k を渡す）ため、サンプル由来の `AttributeError` を回避できる。
   inner graph の config 注入シームは部品の `DomainWorkflowGraphNode.__init__(subgraph_factory=...)` /
   `_parent_config` adaptation point で埋める。
2. **HITL checkpointer**: 既定の `src/api/server.py` は `agent.compile()`（引数なし）だが、
   `hitl.enabled: true` では `agent.compile(checkpointer=...)` が必要。**server.py の改修が必要**
   （resume エンドポイント・thread_id 継承含む）。
3. `emit_trace_event(event_type, payload, state)` は 3 引数。`from shared.utils.audit_logger import emit_trace_event`。
4. secrets は `from shared.secrets import factory` で通る。node 内は `ctx.secrets.require("KEY")`（`os.environ` 禁止）。
5. `execute(self, state)` は**変更したキーだけ返す**（full state 返却は node_history reducer を二重化）。
   `status` は `AgentStatus.<X>.value`（文字列）。
6. TrustLevel は文字列 enum（ANONYMOUS/VERIFIED_EXTERNAL/INTERNAL）。DB/KB アクセスする privileged ノードは
   `required_trust_level = VERIFIED_EXTERNAL` を明示宣言。

## PoC 成功指標

通関士差し戻し率、根拠リンク率、一次チェック後の補正時間、誤指摘率で評価。
便益は申告受理・関税額・許認可取得を保証しない（一次チェック限定）。
