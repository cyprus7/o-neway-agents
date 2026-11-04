"""Feature019 frozen baseline: source-independent corpus and actual app fixture path."""
import hashlib
import json
import pytest
from scripts.evaluate_agent import (CORPUS, CORPUS_FILES, canonical_corpus_bytes,
    corpus_fingerprints, fixture_run, load_cases, validate_corpus, verify_corpus_files)


def test_frozen_corpus_coverage_and_split():
    cases=load_cases()
    validate_corpus(cases)
    manifest=json.loads((CORPUS / "manifest.v1.json").read_text())
    for name,digest in manifest["files"].items():
        assert hashlib.sha256(canonical_corpus_bytes((CORPUS / name).read_bytes())).hexdigest()==digest
    assert verify_corpus_files()['files']==manifest['files']
    assert sum(c['split']=='held_out' for c in cases)==24
    assert all(c['unknown_is_useful'] == (c['capability']=='clarification') for c in cases)


def test_current_fixture_is_deterministic_and_reports_missing_capabilities():
    cases=load_cases()
    first,second=fixture_run(cases),fixture_run(cases)
    def stable(rows):
        variable={'elapsed_seconds','first_useful_recommendation_seconds','fixture_telegram_http_calls'}
        return [{k:v for k,v in row.items() if k not in variable} for row in rows]
    assert stable(first)==stable(second)
    assert sum(row['status']=='passed' for row in first)==30
    assert not any(row['status']=='pending' for row in first)
    assert not any(row['failures'] for row in first)
    assert all(row['model_dispatches']==0 for row in first)
    assert sum(row['fixture_intent_calls'] for row in first)>16
    assert sum(row['fixture_data_calls'] for row in first)>0
    assert all(row['first_useful_recommendation_seconds'] is None for row in first if row['status']=='pending')


def test_historical_baseline_keeps_original_completion_denominator():
    historical=json.loads((CORPUS/'baseline.fixture.v1.json').read_text())
    assert historical['passed']==12 and historical['pending']==18 and historical['failed']==0
    assert historical['completion_denominator']==30


def test_telegram_fixture_uses_https_cookie_delivery_without_manual_token_headers(monkeypatch):
    from scripts import evaluate_agent
    authenticate=evaluate_agent.authenticate_guest
    observed=[]
    def checked(client):
        observed.append(client.base_url.scheme)
        assert client.base_url.scheme=='https'
        assert 'Authorization' not in client.headers and 'Cookie' not in client.headers
        return authenticate(client)
    monkeypatch.setattr(evaluate_agent,'authenticate_guest',checked)
    cases=[case for case in load_cases() if case['id'] in {'A23','A24','A27'}]
    results=fixture_run(cases)
    assert observed and len(results)==3
    assert all(row['status']=='passed' and not row['failures'] for row in results)


def test_frozen_corpus_validates_identically_with_git_lf_or_windows_crlf(tmp_path):
    folders=[]
    for label,newline in [('git-archive',b'\n'),('windows-checkout',b'\r\n')]:
        folder=tmp_path/label;folder.mkdir();folders.append(folder)
        for name in (*CORPUS_FILES,'manifest.v1.json'):
            normalized=canonical_corpus_bytes((CORPUS/name).read_bytes())
            (folder/name).write_bytes(normalized.replace(b'\n',newline))
    left,right=[verify_corpus_files(folder) for folder in folders]
    assert left['canonical_sha256']==right['canonical_sha256']
    assert left['files']==right['files']
    assert left['raw_sha256']!=right['raw_sha256']
    assert load_cases(folders[0])==load_cases(folders[1])==load_cases()


@pytest.mark.parametrize('invalid',[b'\xef\xbb\xbf{}\n',b'{\r}\n',b'\xff\n'])
def test_canonical_corpus_rejects_bom_lone_cr_and_non_utf8(invalid):
    with pytest.raises(ValueError): canonical_corpus_bytes(invalid)


def test_newline_portability_does_not_allow_changed_frozen_input(tmp_path):
    for name in (*CORPUS_FILES,'manifest.v1.json'):
        (tmp_path/name).write_bytes(canonical_corpus_bytes((CORPUS/name).read_bytes()))
    target=tmp_path/CORPUS_FILES[0]
    original=target.read_bytes();changed=original.replace(b'Where',b'WHERE',1)
    assert changed!=original
    target.write_bytes(changed)
    with pytest.raises(ValueError,match='frozen_corpus_checksum_mismatch'):
        load_cases(tmp_path)
