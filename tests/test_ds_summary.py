"""Run in-container:
    docker cp tests/test_ds_summary.py sqlbot:/tmp/
    docker exec sqlbot sh -c "cd /opt/sqlbot/app && .venv/bin/python -m pytest /tmp/test_ds_summary.py -q"
"""
from apps.datasource.crud.table import should_replace_description, generate_ds_summary


def test_should_replace_description_true_cases():
    assert should_replace_description(None) is True
    assert should_replace_description('') is True
    assert should_replace_description('   ') is True
    assert should_replace_description('Excel file: foo.xlsx') is True
    assert should_replace_description('  Excel file: bar.csv  ') is True


def test_should_replace_description_false_for_user_text():
    assert should_replace_description('A dataset of movies with title, cast, plot') is False
    # "Excel file:" mid-string must NOT be treated as the generic auto prefix
    assert should_replace_description('Merged data. Excel file: source.xlsx included.') is False


def test_generate_ds_summary_strips_preamble_label():
    assert generate_ds_summary('x', 'ctx', lambda p: 'Here is a summary: It holds sales.',
                               maxlen=600) == 'It holds sales.'
    assert generate_ds_summary('x', 'ctx', lambda p: 'Summary: It holds games.',
                               maxlen=600) == 'It holds games.'


def test_generate_ds_summary_empty_on_none_output():
    assert generate_ds_summary('x', 'ctx', lambda p: None, maxlen=600) == ''


def test_generate_ds_summary_builds_prompt_and_trims():
    captured = {}
    def fake_llm(prompt):
        captured['p'] = prompt
        return '  A dataset of Netflix movies and shows: title, cast, description.  '
    out = generate_ds_summary('col', '# Table: Movies\n(title:text)\nsample: "Money Heist"',
                              fake_llm, maxlen=600)
    assert 'col' in captured['p'] and 'Movies' in captured['p']
    assert out == 'A dataset of Netflix movies and shows: title, cast, description.'


def test_generate_ds_summary_caps_length():
    out = generate_ds_summary('x', 'ctx', lambda p: 'z' * 1000, maxlen=50)
    assert len(out) <= 50


def test_generate_ds_summary_empty_on_error():
    def boom(prompt):
        raise RuntimeError('llm down')
    assert generate_ds_summary('x', 'ctx', boom, maxlen=600) == ''


def test_generate_ds_summary_empty_on_blank_output():
    assert generate_ds_summary('x', 'ctx', lambda p: '   ', maxlen=600) == ''
