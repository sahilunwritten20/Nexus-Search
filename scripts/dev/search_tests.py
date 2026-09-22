import glob, re

patterns = [
    (r'candidates', 'candidates'),
    (r'debug.*true|debug=True', 'debug'),
    (r'vector_score', 'vector_score'),
    (r'phrase.*vector|vector.*phrase', 'phrase+vector'),
    (r'degraded', 'degraded'),
    (r'crawler.*semantic|semantic.*crawler', 'crawler+semantic'),
    (r'benchmark.*coverage', 'benchmark+coverage'),
    (r'coverage.*100', 'coverage+100'),
]

for f in glob.glob('tests/**/*.py', recursive=True):
    try:
        with open(f, 'r', encoding='utf-8', errors='ignore') as fp:
            content = fp.read()
            for pattern, label in [(p, l) for p, l in [
                (r'candidates', 'candidates'),
                (r'debug.*true|debug=True', 'debug'),
                (r'vector_score', 'vector_score'),
                (r'phrase.*vector|vector.*phrase', 'phrase+vector'),
                (r'degraded', 'degraded'),
                (r'crawler.*semantic|semantic.*crawler', 'crawler+semantic'),
                (r'benchmark.*coverage', 'benchmark+coverage'),
                (r'coverage.*100', 'coverage+100'),
            ]]:
                if re.search(pattern, content, re.IGNORECASE):
                    print(f'{f}: {label}')
    except Exception as e:
        pass