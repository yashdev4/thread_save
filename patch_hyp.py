import re

with open('tests/test_hypothesis_v2.py', 'r', encoding='utf-8') as f:
    content = f.read()

ops_replacement = """@st.composite
def operations(draw):
    t = draw(st.sampled_from(["new", "retry", "repeat", "skip", "backfill", "opt-out", "pause"]))
    query = draw(st.text())
    resp = draw(st.text())
    
    if draw(st.booleans()):
        query = "<!-- /turn i=1 -->" + query
    if draw(st.booleans()):
        resp += " SECRET_API_KEY_XYZ123 "
    if draw(st.integers(0, 10)) == 0:
        query = "A" * 105000
        
    c = draw(st.one_of(st.none(), st.integers(1, 10)))
    return {"type": t, "query": query, "resp": resp, "client_turn": c}
"""

if "@st.composite" in content:
    content = re.sub(r'@st\.composite.*?return \{"type": t, "query": q, "resp": r, "client_turn": c\}', ops_replacement, content, flags=re.DOTALL)
    with open('tests/test_hypothesis_v2.py', 'w', encoding='utf-8') as f:
        f.write(content)
