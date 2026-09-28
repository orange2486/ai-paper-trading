from papertrade import decide


def test_extract_last_valid_json_block():
    text = "說明\n```json\n{bad}\n```\n中間\n```json\n{\"date\": \"2026-07-01\", \"buys\": []}\n```\n"
    assert decide.extract_proposal(text) == {"date": "2026-07-01", "buys": []}
    assert decide.extract_proposal("沒有程式碼區塊") is None
