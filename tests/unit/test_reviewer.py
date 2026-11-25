import json

import pytest

from samurai_ai.domain.reviewer import (
    MAX_REVIEW_NOTES,
    ReviewerError,
    ReviewSeverity,
    parse_review_notes,
)


def test_parse_fenced_notes_and_bound_their_count() -> None:
    payload = {
        "notes": [
            {"severity": "concern", "text": f"note {index}"}
            for index in range(MAX_REVIEW_NOTES + 2)
        ]
    }
    notes = parse_review_notes(f"```json\n{json.dumps(payload)}\n```")
    assert len(notes) == MAX_REVIEW_NOTES
    assert notes[0].severity is ReviewSeverity.CONCERN
    assert notes[-1].text == "note 3"


def test_parse_rejects_prose_and_invalid_notes() -> None:
    with pytest.raises(ReviewerError):
        parse_review_notes("looks mostly fine")
    with pytest.raises(ReviewerError):
        parse_review_notes('{"notes":[{"severity":"stop","text":"x"}]}')
    with pytest.raises(ReviewerError):
        parse_review_notes('{"notes":[{"severity":"info","text":""}]}')
