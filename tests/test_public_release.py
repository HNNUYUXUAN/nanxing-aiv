import json
import io
import os
from pathlib import Path
import subprocess
import zipfile

import pytest

from scripts.build_public_release import (
    AIV_FILES, BASE_FILES, NOTEBOOK_FILES, SYNTHETIC_FILES, CONCEPT_INITIAL_PROMPTS,
    CONCEPT_REVISION_PROMPTS, CONCEPT_PROMPT_FILES, build_release, json_bytes, relative_path,
    secret_findings, sha, scan_sensitive, sensitive_inventory, verify_release, formal_gate, inspect_text,
)
from scripts.run_all import verify_package


def fixture_root(tmp_path):
    root = tmp_path / 'workspace'
    root.mkdir()
    for name in BASE_FILES + AIV_FILES + SYNTHETIC_FILES:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{}\n' if path.suffix == '.json' else '# synthetic fixture\n', 'utf-8')
    (root / 'results/synthetic/provenance.json').write_bytes(json_bytes({
        'source': 'synthetic', 'real_effect_claim': False}))
    (root / 'aiv/__init__.py').write_text('', 'utf-8')
    (root / 'notebooks').mkdir()
    notebook = json_bytes({'cells': [
        {'cell_type': 'markdown', 'source': ['# Synthetic example'],
         'attachments': {'private.png': {'image/png': 'PRIVATE_ATTACHMENT'}},
         'metadata': {'private': 'PRIVATE_METADATA'}},
        {'cell_type': 'code', 'source': ['print(1)'], 'metadata': {},
         'execution_count': 3, 'outputs': [{'text': 'PRIVATE_OUTPUT'}]},
    ], 'metadata': {'private': 'PRIVATE_METADATA'}})
    for name in NOTEBOOK_FILES:
        (root / name).write_bytes(notebook)
    (root / 'data').mkdir()
    (root / 'data/private.csv').write_text('PRIVATE_RAW', 'utf-8')
    return root


def target(root, suffix='v1'):
    return root / 'runtime/public-release' / suffix


def test_release_excludes_history_raw_data_outputs_attachments_and_preserves_hashes(tmp_path):
    root = fixture_root(tmp_path)
    receipt = build_release(root, target(root))
    assert receipt['published'] is False and receipt['publication_ready'] is False
    assert receipt['license_status'] == 'owner_selection_pending'
    with zipfile.ZipFile(receipt['archive']) as archive:
        assert not any(name.startswith(('data/', 'runtime/', '.git/')) for name in archive.namelist())
        nb = archive.read(NOTEBOOK_FILES[0])
        assert not any(value in nb for value in (b'PRIVATE_OUTPUT', b'PRIVATE_METADATA', b'PRIVATE_ATTACHMENT'))
        assert json.loads(archive.read('notebooks/research-inputs.json'))['turn_snapshot'] is None
        manifest = json.loads(archive.read('PUBLIC-RELEASE-MANIFEST.json'))
        for name, record in manifest['files'].items():
            assert sha(archive.read(name)) == record['sha256']
    assert verify_package(target(root))['verified_files'] > 10


def test_known_credential_is_blocked_before_release_files_are_written(tmp_path):
    root = fixture_root(tmp_path)
    secret = 'test-credential-never-export-this'
    (root / '.env').write_text('API_KEY=' + secret, 'utf-8')
    (root / 'aiv/analysis.py').write_text(secret, 'utf-8')
    with pytest.raises(ValueError, match='Secret scan failed') as error:
        build_release(root, target(root))
    assert secret not in str(error.value)
    assert not target(root).exists()
    assert not target(root).with_suffix('.zip').exists()


@pytest.mark.parametrize('name', ['../escape.py', '/absolute.py', 'C:/escape.py',
                                 'aiv/../raw.txt', 'data/source.csv', 'runtime/output.json',
                                 'aiv/.env', 'assets/private.key', 'x\\..\\secret',
                                 'assets/node_modules/a.js'])
def test_paths_fail_closed(name):
    with pytest.raises(ValueError):
        relative_path(name)


def write_review(root, source, destination, content, kind='deidentified_aggregate'):
    path = root / source
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    review = {'schema_version': 1, 'files': [{
        'source': source, 'destination': destination, 'kind': kind, 'sha256': sha(content),
        'review': {'no_personal_data': True, 'no_student_text': True,
                   'redistribution_rights_confirmed': True, 'reviewer': 'fixture reviewer',
                   'rights_basis': 'fixture author approval'},
    }]}
    review_path = root / 'review.json'
    review_path.write_bytes(json_bytes(review))
    return review_path


def test_extra_aggregate_requires_hash_and_rejects_person_level_fields(tmp_path, monkeypatch):
    monkeypatch.setattr('scripts.build_public_release.formal_gate', lambda root: ({'snapshot': 'fixture'}, root))
    root = fixture_root(tmp_path)
    review = write_review(root, 'results/public/aggregate.json',
                          'results/aggregate/result.json', json_bytes({'n': 100, 'mean': .5}))
    receipt = build_release(root, target(root), review)
    assert receipt['reviewed_extras'] == 1
    (root / 'results/public/aggregate.json').write_bytes(json_bytes({'n': 101}))
    with pytest.raises(ValueError, match='Reviewed content changed'):
        build_release(root, target(root, 'v2'), review)
    review = write_review(root, 'results/public/aggregate.json',
                          'results/aggregate/result.json', json_bytes({'student_id': 'S001'}))
    with pytest.raises(ValueError, match='record-level'):
        build_release(root, target(root, 'v3'), review)


def test_review_does_not_override_raw_material_exclusion_or_core_files(tmp_path):
    root = fixture_root(tmp_path)
    review = write_review(root, 'data/raw.json', 'results/aggregate/raw.json', b'{}')
    with pytest.raises(ValueError, match='allowlist'):
        build_release(root, target(root), review)
    review = write_review(root, 'assets/diagram.svg', 'README.md', b'<svg/>', 'illustration')
    with pytest.raises(ValueError, match='public-assets'):
        build_release(root, target(root), review)


def test_existing_license_is_preserved_without_implicitly_authorizing_publication(tmp_path):
    root = fixture_root(tmp_path)
    (root / 'LICENSE').write_text('Existing project license fixture', 'utf-8')
    receipt = build_release(root, target(root))
    assert receipt['license_status'] == 'existing_license_included'
    assert receipt['publication_ready'] is False
    assert (target(root) / 'LICENSE').read_text('utf-8') == 'Existing project license fixture'
    with pytest.raises(ValueError, match='already exists'):
        build_release(root, target(root))


def test_private_key_marker_reports_only_category():
    content = b'-----BEGIN ' + b'RSA PRIVATE KEY-----\nprivate material'
    assert secret_findings(content, []) == ['private_key_material']


def test_new_files_do_not_silently_expand_allowlist(tmp_path):
    root = fixture_root(tmp_path)
    (root / 'aiv/unreviewed.py').write_text('PRIVATE_UNREVIEWED', 'utf-8')
    (root / 'notebooks/unreviewed.ipynb').write_text('PRIVATE_UNREVIEWED', 'utf-8')
    receipt = build_release(root, target(root))
    with zipfile.ZipFile(receipt['archive']) as archive:
        assert 'aiv/unreviewed.py' not in archive.namelist()
        assert 'notebooks/unreviewed.ipynb' not in archive.namelist()
        assert archive.read('.gitattributes') == b'* -text\n'
        assert "connect-src 'none'" in archive.read('demo/index.html').decode()
        assert json.loads(archive.read('notebooks/research-inputs.json'))['turn_plan'] is None
    recorded = json.loads(target(root).with_name('v1.acceptance.json').read_text('utf-8'))
    assert recorded['status'] == 'prepared_local_only' and recorded['verification']['sha256']


@pytest.mark.parametrize('category', ['student_identifier', 'student_text'])
def test_actual_private_values_in_notebook_source_are_blocked_without_echo(tmp_path, category):
    root = fixture_root(tmp_path)
    value = 'private-student-726413' if category == 'student_identifier' else 'This is a private source question with distinctive words.'
    p = root / 'runtime/research/turns-v1/student_turns.jsonl'
    p.parent.mkdir(parents=True)
    p.write_text(json.dumps({'student_id': value if category == 'student_identifier' else 'student-x',
                             'text': value if category == 'student_text' else ''}) + '\n', 'utf-8')
    nb = json.loads((root / NOTEBOOK_FILES[0]).read_text('utf-8'))
    nb['cells'][0]['source'] = [value]
    (root / NOTEBOOK_FILES[0]).write_bytes(json_bytes(nb))
    with pytest.raises(ValueError, match='Sensitive value scan failed') as error:
        build_release(root, target(root))
    assert value not in str(error.value) and not target(root).exists()


def test_sensitive_json_escapes_and_split_source_are_scanned():
    value = 'Unique private sentence split across two source lines.'
    content = json_bytes({'cells': [{'source': [value[:24], value[24:]]}]})
    with pytest.raises(ValueError, match='student_text'):
        scan_sensitive({'notebook.ipynb': content}, {'student_text': {''.join(value.split())}})


def test_reviewed_webp_is_parsed_as_an_image_not_utf8_text():
    from PIL import Image
    stream = io.BytesIO(); Image.new('RGB', (16, 16), 'white').save(stream, format='WEBP')
    assert isinstance(inspect_text('public-assets/diagram.webp', stream.getvalue()), str)


def svg_fixture(content):
    return ('<svg xmlns="http://www.w3.org/2000/svg">' + content + '</svg>').encode()


def test_svg_group_geometry_does_not_create_identifier_matches():
    content = svg_fixture('<g transform="translate(10.654321 20) scale(0.1 -0.1)"><path d="M 0 0 L 1 1"/></g>')
    result = scan_sensitive({'figure.svg': content}, {'student_identifier': {'654321'}})
    assert result['passed']


@pytest.mark.parametrize('content', [
    '<text>654321</text>',
    '<title>654321</title>',
    '<desc>654321</desc>',
    '<metadata><date>2026-09-27T10:20:30.654321</date></metadata>',
    '<!-- 654321 -->',
    '<g data-student="654321"/>',
    '<g transform="translate(1.654321 20 malformed)"/>',
    '<g transform="translate(1.654321,,20)"/>',
    '<g transform="translate(1.654321 20),"/>',
    '<g transform="matrix(1.654321 2)"/>',
    '<path transform="translate(1.654321 20)"/>',
    '<text xmlns:private="https://example.org/654321">label</text>',
])
def test_svg_text_metadata_and_unrecognized_geometry_remain_scanned(content):
    with pytest.raises(ValueError, match='student_identifier') as error:
        scan_sensitive({'figure.svg': svg_fixture(content)}, {'student_identifier': {'654321'}})
    assert '654321' not in str(error.value)


def test_svg_credentials_are_scanned_in_original_geometry_bytes():
    content = svg_fixture('<g transform="translate(10.654321 20)"/>')
    with pytest.raises(ValueError, match='credential'):
        scan_sensitive({'figure.svg': content}, {'credential': {'654321'}})


def test_formal_aggregate_cannot_use_review_booleans_to_bypass_frozen_gate(tmp_path):
    root = fixture_root(tmp_path)
    (root / 'notebooks/research-inputs.json').write_bytes(json_bytes({'turn_snapshot': None}))
    review = write_review(root, 'paper/aggregate_results.json', 'results/aggregate/results.json', b'{"n":100}')
    with pytest.raises(ValueError, match='pinned frozen'):
        build_release(root, target(root), review)
    with pytest.raises(ValueError, match='pinned frozen'):
        build_release(root, target(root), include_paper=True)
    assert not target(root).exists()


def test_concept_images_require_complete_acceptance_and_no_broken_markdown(tmp_path, monkeypatch):
    root = fixture_root(tmp_path)
    monkeypatch.setattr('scripts.package_research.concept_illustration_inputs', lambda root: {'status': 'pending'})
    with pytest.raises(ValueError, match='five concept'):
        build_release(root, target(root), include_concepts=True)
    p = root / NOTEBOOK_FILES[0]
    nb = json.loads(p.read_text('utf-8'))
    nb['cells'].append({'cell_type': 'markdown', 'metadata': {},
                        'source': '<!-- concept-illustration-v1:example -->\n![figure](../assets/illustrations/example.png)'})
    p.write_bytes(json_bytes(nb))
    result = build_release(root, target(root))
    assert b'concept-illustration-v1:example' not in (target(root) / NOTEBOOK_FILES[0]).read_bytes()
    assert result['concept_illustrations']['status'] == 'not_included'


def test_unspecified_missing_image_fails_instead_of_shipping_bad_link(tmp_path):
    root = fixture_root(tmp_path)
    p = root / NOTEBOOK_FILES[0]
    nb = json.loads(p.read_text('utf-8')); nb['cells'][0]['source'] = ['![private](../missing.png)']
    p.write_bytes(json_bytes(nb))
    with pytest.raises(ValueError, match='image is not included'):
        build_release(root, target(root))
    assert not target(root).exists()


def test_manifest_rejects_tampered_directory_and_extra_private_file(tmp_path):
    root = fixture_root(tmp_path); build_release(root, target(root))
    entries = {p.relative_to(target(root)).as_posix(): p.read_bytes() for p in target(root).rglob('*') if p.is_file()}
    entries['aiv/analysis.py'] += b'changed'
    with pytest.raises(ValueError, match='hash mismatch'):
        verify_release(entries)
    entries['data/qa.csv'] = b'private'
    with pytest.raises(ValueError, match='allowlist'):
        verify_release(entries)


def test_write_failure_gets_private_failed_receipt(tmp_path, monkeypatch):
    root = fixture_root(tmp_path)
    def fail(*args, **kwargs):
        raise OSError('fixture archive failure')
    monkeypatch.setattr('scripts.build_public_release.zipfile.ZipFile', fail)
    with pytest.raises(OSError):
        build_release(root, target(root))
    receipt = json.loads(target(root).with_name('v1.acceptance.json').read_text('utf-8'))
    assert receipt['status'] == 'failed' and receipt['error_category'] == 'OSError'


def test_git_add_and_checkout_preserve_manifest_bytes_even_with_autocrlf(tmp_path):
    root = fixture_root(tmp_path); build_release(root, target(root))
    directory = target(root)
    env = {**os.environ, 'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_CONFIG_SYSTEM': os.devnull}
    def git(*args):
        return subprocess.run(['git', '-c', 'core.autocrlf=true', *args], cwd=directory, env=env,
                              check=True, capture_output=True).stdout
    git('init', '-q')
    git('add', '--all')
    manifest = json.loads((directory / 'PUBLIC-RELEASE-MANIFEST.json').read_text('utf-8'))
    for name, record in manifest['files'].items():
        assert sha(git('show', ':' + name)) == record['sha256']
    checkout = tmp_path / 'checkout'; checkout.mkdir()
    git('checkout-index', '--all', '--prefix=' + checkout.as_posix() + '/')
    assert all(sha((checkout / name).read_bytes()) == record['sha256'] for name, record in manifest['files'].items())


def test_formal_gate_binds_existing_visual_and_statistical_receipts(tmp_path, monkeypatch):
    root = fixture_root(tmp_path)
    frozen = root / 'runtime/research/example/t3/frozen'; frozen.mkdir(parents=True)
    for name in ('annotations.json', 'summary.json'):
        (frozen / name).write_bytes(b'{}')
    snapshot = {'files': {n: sha((frozen / n).read_bytes()) for n in ('annotations.json', 'summary.json')}}
    (frozen / 'snapshot-manifest.json').write_bytes(json_bytes(snapshot))
    pin = sha((frozen / 'snapshot-manifest.json').read_bytes())
    materials = root / 'runtime/research/materials'; materials.mkdir()
    (materials / 'summary.json').write_bytes(b'{}')
    (materials / 'manifest.json').write_bytes(json_bytes({'source': {'manifest_sha256': pin}, 'files': {'summary.json': sha(b'{}')}}))
    material_pin = sha((materials / 'manifest.json').read_bytes())
    (root / 'notebooks/research-inputs.json').write_bytes(json_bytes({
        'turn_snapshot': {'manifest_path': (frozen / 'snapshot-manifest.json').relative_to(root).as_posix(), 'manifest_sha256': pin},
        'turn_materials': {'manifest_path': (materials / 'manifest.json').relative_to(root).as_posix(), 'manifest_sha256': material_pin}}))
    (frozen / 'cache-lineage-audit.json').write_bytes(json_bytes({'passed': True}))
    stat_path = root / 'results/model-verification/full-turn-reporting-t3.json'; stat_path.parent.mkdir()
    stat = {'passed': True, 'checks': {'independent_cache_lineage': True},
            'source_manifest_sha256': pin, 'materials_manifest_sha256': material_pin,
            'verifier_sha256': sha((root / 'scripts/verify_full_turn_materials.py').read_bytes()),
            'cache_receipt_sha256': sha((frozen / 'cache-lineage-audit.json').read_bytes())}
    stat_path.write_bytes(json_bytes(stat))
    visual = root / 'build/paper/validation.json'; visual.parent.mkdir(parents=True)
    (visual.parent / 'main.pdf').write_bytes(b'fixture accepted pdf')
    visual.write_bytes(json_bytes({'paper_pdf_sha256': sha(b'fixture accepted pdf')}))
    calls = []
    monkeypatch.setattr('scripts.package_research.validate_delivery', lambda *args: calls.append(args))
    accepted, _ = formal_gate(root)
    assert calls == [(root, pin, material_pin)] and accepted['snapshot_manifest_sha256'] == pin
    stat['materials_manifest_sha256'] = 'stale'; stat_path.write_bytes(json_bytes(stat))
    with pytest.raises(ValueError, match='statistical acceptance'):
        formal_gate(root)


def add_accepted_concepts(root):
    from PIL import Image
    from scripts.attach_concept_illustrations import FIGURE_IDS
    directory = root / 'assets/illustrations'; directory.mkdir(parents=True)
    figures = []
    for name in FIGURE_IDS:
        p = directory / (name + '.png'); Image.new('RGB', (640, 400), 'white').save(p)
        figures.append({'id': name, 'file': p.relative_to(root).as_posix(), 'qa': 'passed',
                        'title': 'Synthetic concept', 'alt': 'Synthetic concept figure',
                        'caption': 'Conceptual fixture, without empirical results.',
                        'sha256': sha(p.read_bytes()), 'native_dimensions': [640, 400],
                        'prompt': CONCEPT_INITIAL_PROMPTS[name],
                        'generation': ({'revision_prompt': CONCEPT_REVISION_PROMPTS[name]}
                                       if name in CONCEPT_REVISION_PROMPTS else {}),
                        'notebooks': [NOTEBOOK_FILES[0]], 'sources': ['aiv/models.py']})
    manifest = {'schema_version': 'nanxing-concept-illustrations-v1', 'kind': 'conceptual',
                'generation_surface': 'fixture', 'acceptance': ['fixture visual review'], 'figures': figures}
    (directory / 'manifest.json').write_bytes(json_bytes(manifest))
    for name in CONCEPT_PROMPT_FILES:
        path = root / name; path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(('Synthetic conceptual prompt: ' + path.stem).encode())
    return manifest


def test_cleaned_notebook_supports_real_concept_attachment_without_private_cell_ids(tmp_path):
    import nbformat
    from scripts.attach_concept_illustrations import attach
    root = fixture_root(tmp_path); add_accepted_concepts(root)
    path = root / NOTEBOOK_FILES[0]
    nb = json.loads(path.read_bytes()); nb['cells'][0]['id'] = 'PRIVATE_ORIGINAL_ID'
    path.write_bytes(json_bytes(nb))
    result = build_release(root, target(root), include_concepts=True)
    public = Path(result['directory'])
    assert b'PRIVATE_ORIGINAL_ID' not in (public / NOTEBOOK_FILES[0]).read_bytes()
    assert json.loads((public / NOTEBOOK_FILES[0]).read_bytes())['nbformat_minor'] == 5
    assert attach(public, apply=True)['code_cells_preserved'] is True
    notebook = nbformat.read(public / NOTEBOOK_FILES[0], as_version=4)
    nbformat.validate(notebook)
    assert len([c for c in notebook.cells if c.cell_type == 'markdown' and
                '<!-- concept-illustration-v1:' in c.source]) == 5


def test_all_five_concepts_use_exact_reviewed_source_hashes_without_mirrors(tmp_path):
    from scripts.attach_concept_illustrations import FIGURE_IDS
    root = fixture_root(tmp_path); add_accepted_concepts(root)
    directory = root / 'assets/illustrations'
    (directory / 'prompts/unreviewed.txt').write_bytes(b'Unreviewed prompt must not ship')
    notebook_path = root / NOTEBOOK_FILES[0]
    notebook = json.loads(notebook_path.read_bytes())
    notebook['cells'].append({'cell_type': 'markdown', 'metadata': {'concept': 'source metadata'},
                              'source': '<!-- concept-illustration-v1:process-overview -->\n![flow](../assets/illustrations/process-overview.png)'})
    notebook_path.write_bytes(json_bytes(notebook))
    result = build_release(root, target(root), include_concepts=True)
    assert result['concept_illustrations']['images'] == 5
    assert set(result['concept_prompt_source_hashes']) == set(CONCEPT_PROMPT_FILES)
    assert all(result['source_hashes'][name] == digest == sha((root / name).read_bytes())
               for name, digest in result['concept_prompt_source_hashes'].items())
    with zipfile.ZipFile(result['archive']) as archive:
        assert len([n for n in archive.namelist() if n.endswith('.png')]) == 5
        assert not any(n.startswith(('build/', 'deliverables/')) for n in archive.namelist())
        assert 'assets/illustrations/prompts/unreviewed.txt' not in archive.namelist()
        assert set(n for n in archive.namelist() if '/prompts/' in n) == set(CONCEPT_PROMPT_FILES)
        assert json.loads(archive.read('PUBLIC-RELEASE-MANIFEST.json'))['concept_prompt_source_hashes'] == result['concept_prompt_source_hashes']
        readme = archive.read('README.md').decode()
        assert all(f'](assets/illustrations/{figure}.png)' in readme for figure in FIGURE_IDS)
        nb = json.loads(archive.read(NOTEBOOK_FILES[0]))
        assert any('<!-- concept-illustration-v1:process-overview -->' in cell['source'] for cell in nb['cells'])
        assert all(not cell['metadata'] for cell in nb['cells'])
    (directory / (FIGURE_IDS[0] + '.png')).write_bytes(b'changed')
    with pytest.raises(ValueError, match='acceptance'):
        build_release(root, target(root, 'v2'), include_concepts=True)


@pytest.mark.parametrize('field', ['prompt', 'revision_prompt'])
def test_manifest_cannot_expand_fixed_prompt_allowlist(tmp_path, field):
    root = fixture_root(tmp_path); manifest = add_accepted_concepts(root)
    row = manifest['figures'][0]
    if field == 'prompt':
        row[field] = 'assets/illustrations/prompts/unreviewed.txt'
    else:
        row['generation'][field] = 'assets/illustrations/prompts/unreviewed.txt'
    (root / 'assets/illustrations/manifest.json').write_bytes(json_bytes(manifest))
    with pytest.raises(ValueError, match='fixed reviewed allowlist'):
        build_release(root, target(root), include_concepts=True)
    assert not target(root).exists()


def test_prompt_receives_same_actual_student_text_scan_as_code(tmp_path):
    root = fixture_root(tmp_path); add_accepted_concepts(root)
    value = 'Distinctive private student question must never be public.'
    p = root / 'runtime/research/turns-v1/student_turns.jsonl'; p.parent.mkdir(parents=True)
    p.write_text(json.dumps({'student_id': 'student-x', 'text': value}) + '\n', 'utf-8')
    (root / CONCEPT_PROMPT_FILES[-1]).write_text(value, 'utf-8')
    with pytest.raises(ValueError, match='Sensitive value scan failed') as error:
        build_release(root, target(root), include_concepts=True)
    assert value not in str(error.value) and not target(root).exists()


def test_prompt_changed_during_collection_cannot_borrow_recorded_hash(tmp_path, monkeypatch):
    from scripts import build_public_release as builder
    root = fixture_root(tmp_path); add_accepted_concepts(root)
    original = builder.scan_sensitive
    def change_after_scan(entries, values):
        result = original(entries, values)
        (root / CONCEPT_PROMPT_FILES[0]).write_bytes(b'Changed during collection')
        return result
    monkeypatch.setattr(builder, 'scan_sensitive', change_after_scan)
    with pytest.raises(ValueError, match='Source changed during release collection'):
        build_release(root, target(root), include_concepts=True)
    assert not target(root).exists()


def test_readme_missing_image_is_blocked_before_write(tmp_path, monkeypatch):
    root = fixture_root(tmp_path)
    monkeypatch.setattr('scripts.build_public_release.candidate_readme',
                        lambda *args, **kwargs: b'![missing](assets/missing.png)')
    with pytest.raises(ValueError, match='image is not included'):
        build_release(root, target(root))
    assert not target(root).exists()


@pytest.mark.parametrize('when', ['after_gate', 'during_scan'])
def test_parallel_pdf_rebuild_cannot_borrow_an_old_visual_receipt(tmp_path, monkeypatch, when):
    root = fixture_root(tmp_path)
    pdf = root / 'build/paper/main.pdf'; pdf.parent.mkdir(parents=True); pdf.write_bytes(b'accepted bytes')
    accepted_hash = sha(pdf.read_bytes())
    def gate(_):
        if when == 'after_gate':
            pdf.write_bytes(b'unreviewed rebuild')
        return {'paper_pdf_sha256': accepted_hash}, root
    def scan(*args):
        if when == 'during_scan':
            pdf.write_bytes(b'unreviewed rebuild')
        return {'passed': True}
    monkeypatch.setattr('scripts.build_public_release.formal_gate', gate)
    monkeypatch.setattr('scripts.build_public_release.scan_sensitive', scan)
    with pytest.raises(ValueError, match='PDF changed during'):
        build_release(root, target(root), include_paper=True)
    assert not target(root).exists()
