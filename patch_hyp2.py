import re

with open('tests/test_hypothesis_v2.py', 'r', encoding='utf-8') as f:
    text = f.read()

ops_replacement = """@st.composite
def operations(draw):
    op_type = draw(st.sampled_from(["new", "retry", "repeat", "skip", "backfill", "opt-out", "pause"]))
    q = draw(st.text())
    r = draw(st.text())
    
    if draw(st.booleans()):
        q = "<!-- /turn i=1 -->" + q
    if draw(st.booleans()):
        if r:
            r += " SECRET_API_KEY_XYZ123 "
        else:
            r = " SECRET_API_KEY_XYZ123 "
    if draw(st.integers(0, 10)) == 0:
        q = "A" * 105000
        
    c = draw(st.one_of(st.none(), st.integers(1, 10)))
    return {"type": op_type, "query": q, "resp": r, "client_turn": c}"""

text = re.sub(r'@st\.composite.*?return \{"type": op_type, "query": q, "resp": r, "client_turn": c\}', ops_replacement, text, flags=re.DOTALL)

with open('tests/test_hypothesis_v2.py', 'w', encoding='utf-8') as f:
    f.write(text)
