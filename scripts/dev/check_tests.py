import glob

patterns = [
    'fusion=weighted',
    'fusion_weighted', 
    'weighted.*fusion',
    'candidates',
    'debug.*true',
    'debug=True',
    'vector_score',
    'phrase.*vector',
    'vector.*phrase',
    'degraded',
    'crawler.*semantic',
    'semantic.*crawler',
    'benchmark.*coverage',
    'coverage.*100',
]

for f in glob.glob('tests/**/*.py', recursive=True):
    try:
        with open(f, encoding='utf-8') as fp:
            content = fp.read()
            for p in patterns:
                if p in content:
                    print(f'{f}: found "{p}"')
    except:
        pass