import json
from datetime import datetime, timedelta, timezone

from integrations import memos_client


def test_ensure_gpt_prefix_keeps_prefixed_content():
    content = "#gpt\n\nHallo"
    assert memos_client.ensure_gpt_prefix(content) == content


def test_ensure_gpt_prefix_adds_prefix():
    assert memos_client.ensure_gpt_prefix("Hallo").startswith("#gpt\n\nHallo")


def test_create_memo_builds_request_body(monkeypatch):
    captured = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps({"name": "memos/123", "content": "#gpt\n\nHallo"}).encode("utf-8")

    def fake_urlopen(req, timeout=0):
        captured["url"] = req.full_url
        captured["method"] = req.get_method()
        captured["headers"] = dict(req.header_items())
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return FakeResponse()

    monkeypatch.setenv("MEMOS_ACCESS_TOKEN", "token123")
    monkeypatch.setenv("MEMOS_BASE_URL", "http://192.0.2.53:5230")
    monkeypatch.setattr(memos_client.request, "urlopen", fake_urlopen)

    result = memos_client.create_memo("Hallo")

    assert result["name"] == "memos/123"
    assert captured["url"] == "http://192.0.2.53:5230/api/v1/memos"
    assert captured["method"] == "POST"
    assert captured["headers"]["Authorization"] == "Bearer token123"
    assert captured["body"]["state"] == "NORMAL"
    assert captured["body"]["visibility"] == "PRIVATE"
    assert captured["body"]["content"].startswith("#gpt")


def test_prepare_gpt_memo_content_puts_tags_at_end():
    content = memos_client.prepare_gpt_memo_content("Hallo", topic="hrv")

    assert content.startswith("#gpt\n\n")
    assert content.splitlines()[0] == "#gpt"
    assert "#hrv" not in content.splitlines()[0]
    assert content.rstrip().endswith("#beobachtung #hrv")


def test_build_memory_tags_for_illness_text():
    tags = memos_client.build_memory_tags("Wenn mein Hals kratzt und der Puls hoch ist.", topic="krankheit recovery")
    assert tags == ["#kurzfristig", "#krankheit"]


def test_build_memory_tags_for_liva_project_text():
    tags = memos_client.build_memory_tags("LIVA API Entscheidung fuer LIVA", topic="liva api")
    assert tags == ["#projekt", "#liva"]


def test_append_terminal_tags_does_not_duplicate():
    content = "#gpt\n\nHallo\n\n#projekt #liva\n"
    assert memos_client.append_terminal_tags(content, ["#projekt", "#liva"]) == content


def test_search_memos_filters_query_and_include_gpt(monkeypatch):
    monkeypatch.setattr(
        memos_client,
        "list_memos",
        lambda page_size=20, page_token=None, state="NORMAL", filter=None, order_by=None: {
            "memos": [
                {"name": "memos/1", "content": "#gpt\n\nTraining note\n\n#beobachtung #training\n", "snippet": "Training", "tags": ["gpt", "memory"]},
                {"name": "memos/2", "content": "Other note", "snippet": "Other", "tags": ["manual"]},
            ]
        },
    )

    result = memos_client.search_memos(query="training", tags=None, limit=20, include_gpt=True)

    assert result["backend"] == "usememos"
    assert result["count"] == 1
    assert result["memos"][0]["name"] == "memos/1"


def test_is_short_term_gpt_memo_true_for_gpt_short_term():
    memo = {"content": "#gpt\n\nHallo\n\n#kurzfristig #krankheit\n"}
    assert memos_client.is_short_term_gpt_memo(memo) is True


def test_is_short_term_gpt_memo_false_for_non_gpt_short_term():
    memo = {"content": "Hallo\n\n#kurzfristig #krankheit\n"}
    assert memos_client.is_short_term_gpt_memo(memo) is False


def test_cleanup_expired_gpt_memos_deletes_only_gpt(monkeypatch):
    old = (datetime.now(timezone.utc) - timedelta(days=15)).isoformat().replace("+00:00", "Z")
    deleted = []
    monkeypatch.setattr(
        memos_client,
        "list_memos",
        lambda page_size=20, page_token=None, state="NORMAL", filter=None, order_by=None: {
            "memos": [
                {"name": "memos/gpt1", "content": "#gpt\n\nA\n\n#kurzfristig #krankheit\n", "createTime": old},
                {"name": "memos/user1", "content": "User memo\n\n#kurzfristig #krankheit\n", "createTime": old},
            ]
        },
    )
    monkeypatch.setattr(memos_client, "delete_memo", lambda name, force=False: deleted.append(name) or {"deleted": True, "name": name})
    result = memos_client.cleanup_expired_gpt_memos(max_age_days=14, dry_run=False)
    assert result["deleted_count"] == 1
    assert deleted == ["memos/gpt1"]


def test_cleanup_ignores_user_memos_even_if_short_term(monkeypatch):
    old = (datetime.now(timezone.utc) - timedelta(days=20)).isoformat().replace("+00:00", "Z")
    monkeypatch.setattr(
        memos_client,
        "list_memos",
        lambda page_size=20, page_token=None, state="NORMAL", filter=None, order_by=None: {
            "memos": [{"name": "memos/user1", "content": "User memo\n\n#kurzfristig #krankheit\n", "createTime": old}]
        },
    )
    result = memos_client.cleanup_expired_gpt_memos(max_age_days=14, dry_run=False)
    assert result["deleted_count"] == 0
    assert result["expired_count"] == 0


def test_normalize_tag_accepts_hash_or_plain():
    assert memos_client.normalize_tag("#krankheit") == "krankheit"
    assert memos_client.normalize_tag("krankheit") == "krankheit"


def test_keyword_search_finds_free_words(monkeypatch):
    monkeypatch.setattr(
        memos_client,
        "list_memos",
        lambda page_size=20, page_token=None, state="NORMAL", filter=None, order_by=None: {
            "memos": [{"name": "memos/1", "content": "#gpt\n\nWenn mein Hals kratzt und der Puls hoch ist.\n", "snippet": "Hals Puls"}]
        },
    )
    result = memos_client.search_memos(query=None, tags=None, keywords=["Hals", "Puls"], include_gpt=True)
    assert result["count"] == 1
