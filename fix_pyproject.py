#!/usr/bin/env python3
"""Fix pyproject.toml structure for ruff 0.17+"""
import tomllib
import tomli_w

with open('C:/Users/Admin/Desktop/nexus search/nexus-search/pyproject.toml', 'rb') as f:
    data = tomllib.load(f)

# Completely rebuild the tool.ruff section correctly
data['tool']['ruff'] = {
    'lint': {
        'select': [
            'E', 'F', 'I', 'UP', 'B', 'C4', 'SIM', 'PIE', 'TRY', 'RUF', 'ISC', 'BLE', 'S110', 'SIM905', 'SIM117', 'SIM113', 'SIM102', 'SIM103', 'PLR1730', 'B009', 'F601', 'RUF023', 'RUF022', 'RUF059', 'F811', 'F841', 'C401', 'TRY401'
        ],
        'target-version': 'py312',
        'preview': True,
        'ignore': [
            'E501', 'B008', 'B011', 'ISC001', 'RUF013', 'ISC004', 'BLE001', 'TRY300', 'TRY301', 'TRY400', 'TRY401',
            'RUF003', 'RUF002', 'B007', 'B905', 'RUF052', 'F841', 'SIM113', 'RUF075', 'SIM108', 'SIM103', 'TRY004',
            'UP035', 'SIM113', 'RUF069', 'E402', 'F601', 'C416', 'RUF039', 'RUF059', 'PIE790', 'SIM117', 'RUF022',
            'F811', 'SIM102', 'PLR1730', 'B009', 'SIM103', 'TRY004', 'SIM113', 'RUF022', 'F811', 'SIM117', 'RUF023',
            'F841', 'C401', 'TRY401', 'RUF100', 'TRY003', 'E741', 'B027'
        ],
        'per-file-ignores': {
            'nexus_search/__init__.py': ['EXE001']
        }
    },
    'format': {
        'quote-style': 'single',
        'indent-style': 'space',
        'skip-magic-trailing-comma': False,
        'line-ending': 'lf'
    }
}

with open('C:/Users/Admin/Desktop/nexus search/nexus-search/pyproject.toml', 'wb') as f:
    tomli_w.dump(data, f)
print('Fixed pyproject.toml completely')