# テスト仕様 — LOG-C2-109

## 実行方法

```sh
source .venv/bin/activate
pytest tests/ -q --tb=line
pytest tests/proof_of_boundary/ -q --tb=line
```

LLM、DB、KB は全て config 注入シームを通し、テストでは fake に差し替える。ネットワーク接続は行わない。

## Framework / Security TC

| ID | 確認内容 | テスト |
|---|---|---|
| TC-01 | flat TypedDict の State 契約 | `test_tc01_state_contract_has_domain_fields_and_no_credentials` |
| TC-02 | S-3 がネストした専門職境界キーを fail-closed | `test_tc02_s3_rejects_nested_professional_boundary_key` |
| TC-03 | credential 風 field を State に置かない | `test_state_safety.py` |
| TC-04 | InvocationContext は framework configurable/state 経由 | graph と `InvocationContext.from_state()` の実機契約、node に context field なし |
| TC-05 | execute 内 lifecycle event 重複なし | PB-6 および node の domain event 検査 |
| TC-06 | S-2 は `_extra_security_gate_input` のみ | `PreProcessNode` 実行経路 |
| TC-07 | S-3 は `_extra_security_gate_output` のみ | `PostProcessNode` 実行経路 |
| TC-08 | VERIFIED_EXTERNAL 未満を S-1 で拒否 | framework node contract |
| TC-09 | standalone/STG bearer が VERIFIED_EXTERNAL を確立し、誤/欠落 bearer は generic 401 | `test_server_auth.py` |
| TC-10 | `/resume` は checkpoint customer 所有権を認証 caller と照合 | `test_server_auth.py` |

## Domain / Security cases

| ID | 期待結果 |
|---|---|
| D-01 | caller→顧客マッピングと packet `customer_id` が不一致なら KB 接続前に error |
| D-02 | caller 未マッピングなら default KB を選ばず error |
| D-03 | source ID 不足、OCR 低信頼、単位不明は `unknowns` と broker review |
| D-04 | checklist の owner/version/effective_from/source 不足は human review |
| D-05 | KB 空振り/ID不足は success + degradation + unknowns、backend 例外は error |
| D-06 | consistency claim は全件 original `source_ids` を持つ |
| D-07 | HS 分類・許認可最終判断・申告・外部送信キー/文言はネストを含め S-3 で拒否 |
| D-08 | `similar_return_reasons` 経由の専門職境界混入も拒否 |

## Proof of boundary

| ID | 確認内容 | テスト |
|---|---|---|
| PB-1 | domain audit event が各 Stage で発行 | Stage node `emit_trace_event` 呼出 |
| PB-2 | State が JSON/msgpack 安全 | `test_state_safety.py` |
| PB-3 | DB/KB/LLM は注入 fake のみ | `test_workflow.py` の FakeKB |
| PB-4 | Level 0 import isolation | `test_import_isolation.py` |
| PB-5 | checkpoint に credential/object を置かない | `test_state_safety.py` と HITL resume |
| PB-5a | raw supplier/unit price/origin が outer/inner InMemorySaver の suspend 中・resume 後 checkpoint にない | `test_payload_checkpoint_safety.py` |
| PB-6 | S-1→S-2→execute→S-3 実行順序 | `test_pb_invoke_order.py` |
| PB-7 | compiled outer graph が Stage5 interrupt を AWAITING_HUMAN として伝播し、`hitl_allowed:false` は中断しない | `test_pb7_hitl_interrupt_propagation.py`、`test_workflow.py` |

## 実行結果（MR-B）

- `pytest tests/ -q --tb=line`: 106 passed
- `pytest tests/proof_of_boundary/ -q --tb=line`: 6 passed

## 実 LLM 1 回転検証

モックだけで固めた実装は実 LLM を繋いだ瞬間に動かないことがあるため、注入シームへ実 LLM クライアントを刺して 1 回 invoke し、**中身のある出力が出ること**を確認している（`gitproject-dev-flow.md` §14-1-bis）。

| 項目 | 内容 |
|---|---|
| 実施日 | 2026-08-18 |
| 使用 LLM | ローカル CLI（Azure OpenAI `gpt-5.5`）を `BaseLLM` 実装として注入 |
| 注入シーム | `config[llm]` |
| 実 LLM 呼び出し | 2 回 |
| 出力 | ['document_completeness', 'consistency_findings', 'similar_return_reasons', 'mode', 'gate_kind'] が非空。縮退理由なし |

本番の LLM は `AnthropicClient` 等で、これは検証用シーム。テンプレの `src/` は変更していない（一時テストを生成→実行→削除する方式）。CI には入れていない（ローカル CLI と鍵が要るため）。
