import sys
sys.path.append('.')
try:
    from google import generativeai
    names = [n for n in dir(generativeai) if not n.startswith('_')]
    print('\n'.join(sorted(names)))
except Exception as e:
    print('ERROR', e)