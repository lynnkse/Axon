"""Dependency map with expandable cards. No external scripts or services."""
import html
import json
import re


def esc(value):
    return html.escape(str(value if value is not None else ''))


def relationships(records, project_name="MR-BSP-Paper"):
    """References are evidence of a link, not automatically evidence of causality."""
    numbers = {}
    for i, row in enumerate(records):
        m = re.search(r'\bExperiment\s*(\d+)\b', row.get('name', ''), re.I)
        if m:
            numbers[int(m.group(1))] = i
    edges = []
    for i, row in enumerate(records):
        text = str(row.get('description') or '') + '\n' + str(row.get('notes') or '')
        seen = set()
        for sentence in re.split(r'(?<=[.!?])\s+|\n', text):
            for match in re.finditer(r'\bExperiment[\s-]*(\d+)\b', sentence, re.I):
                parent = numbers.get(int(match.group(1)))
                if parent is not None and parent < i and parent not in seen:
                    seen.add(parent)
                    edges.append({'source': parent, 'target': i, 'reason': sentence.strip(), 'kind': 'Recorded reference'})
    # Curated transitions: reasons are grounded in the saved descriptions.
    transitions = [
        (2, 3, "Extend the corrected candidate-action comparison to Robot B and separate entropy from path length."),
        (5, 6, "Add the actual independent-action consistency check to the same geometry and data."),
        (9, 10, "Expand the headline comparison to every negotiation iteration using the same geometry."),
        (11, 12, "Introduce multiple landmarks and genuine field-of-view-staged loop closures."),
        (12, 13, "Replace the single-seed trace with a genuine 30-seed aggregate using the same geometry."),
        (13, 14, "Pool the per-iteration results across all 30 seeds instead of showing one representative trace."),
        (14, 15, "Test sampling-based Step-1 decisions in the pooled per-iteration comparison."),
        (19, 20, "Test earlier stopping while retaining the late surveyed-landmark event for full sharing."),
        (19, 21, "Alternative branch: make the late landmark available independently of each seed's stopping time."),
        (20, 22, "Refine the wide stopping-time distribution without an artificial minimum-stop floor."),
        (22, 25, "Evaluate robot-local realized entropy including newly observed execution landmarks; planning unchanged."),
        (22, 26, "Paired execution-entropy ablation excluding newly observed landmarks; planning unchanged."),
        (28, 29, "Expand the epsilon pilot into a matched multi-seed sequential-replanning campaign."),
        (34, 35, "Retain corrected HOLD behavior and shorten the longer route to test asymmetric terminal-phase effects."),
        (35, 36, "Restore ten samples so epsilon .8 and .9 have distinct acceptance predicates."),
        (38, 39, "Test canonical reconstruction across physical trajectories after the frozen-checkpoint order audit."),
        (39, 40, "Separate early-state effects from continued epsilon policy using common session-2 checkpoints."),
        (40, 41, "Audit the exact 49-factor delivery packet, ownership, recipient membership, and provenance before intervention."),
        (41, 42, "Test bounded sufficiency of the audited 49-factor delivery bundle against control, sham, and reference conditions at the frozen checkpoint."),
        (42, 43, "Decompose the successful bundle into packet delivery, belief optimization, and sharing-frontier advancement across six controlled conditions."),
    ]
    for parent, child, reason in (transitions if project_name == "MR-BSP-Paper" else []):
        if parent in numbers and child in numbers:
            a, b = numbers[parent], numbers[child]
            edges = [e for e in edges if (e['source'], e['target']) != (a, b)]
            edges.append({'source': a, 'target': b, 'reason': reason, 'kind': 'Documented follow-up'})
    return edges


def table_markup(data):
    if not isinstance(data, dict) or not data.get('rows'):
        return ''
    columns = data.get('columns') or []
    rows = data['rows']
    if not columns and isinstance(rows[0], dict):
        columns = list(rows[0])
    cells = ''.join('<tr>' + ''.join('<td>' + esc(v) + '</td>' for v in
                    ([row.get(c, '') for c in columns] if isinstance(row, dict) else row)) + '</tr>' for row in rows)
    return '<h4>' + esc(data.get('title', 'Results')) + '</h4><div class="table"><table><thead><tr>' + ''.join('<th>' + esc(c) + '</th>' for c in columns) + '</tr></thead><tbody>' + cells + '</tbody></table></div><p>' + esc(data.get('note', '')) + '</p>'


def render_map(records, project_name=""):
    edges = relationships(records, project_name)
    levels = [0] * len(records)
    for i in range(len(records)):
        parents = [e['source'] for e in edges if e['target'] == i]
        if parents:
            levels[i] = 1 + max(levels[p] for p in parents)
    cards = []
    for i, row in enumerate(records):
        source = row.get('data_source') or {}
        state = source.get('state', '') if isinstance(source, dict) else ''
        incoming = [e for e in edges if e['target'] == i]
        links = ''.join('<p class="reason"><b>From ' + esc(records[e['source']]['name']) + '</b><br>' + esc(e['reason']) + '</p>' for e in incoming)
        images = ''
        for diagram in row.get('diagrams') or []:
            if (row.get('id') == '016eba65-efe9-44ce-84a4-06c6307de5e0'
                    and isinstance(diagram, dict)
                    and diagram.get('kind') == 'availability_notice'):
                images += ('<aside class="figure-availability"><h4>'
                           + esc(diagram.get('section', 'Figure availability'))
                           + '</h4><p>' + esc(diagram.get('caption', ''))
                           + '</p></aside>')
                continue
            if isinstance(diagram, dict) and re.match(r'^(?:https?://|data:image/png;base64,)', str(diagram.get('url', ''))):
                if diagram.get('section'):
                    images += '<h4 class="diagram-section">' + esc(diagram['section']) + '</h4>'
                images += '<figure><img loading="lazy" src="' + esc(diagram['url']) + '"><figcaption>' + esc(diagram.get('caption', '')) + '</figcaption></figure>'
        cards.append('<details class="node" id="n' + str(i) + '"><summary>' + esc(row.get('name', 'Untitled')) + '<small>' + esc(state) + ' · click to expand</small></summary><div class="body">' + ('<p>No predecessor documented in this record.</p>' if not incoming else links) + '<p class="prose">' + esc(row.get('description', '')) + '</p>' + table_markup(row.get('results_table')) + table_markup(row.get('dynamics_table')) + images + '<p class="prose">' + esc(row.get('notes', '')) + '</p><details><summary>Source and provenance</summary><pre>' + esc(json.dumps(source, indent=2, ensure_ascii=False)) + '</pre></details></div></details>')
    ranks = ''.join('<div class="rank">' + ''.join(cards[i] for i in range(len(cards)) if levels[i] == level) + '</div>' for level in sorted(set(levels)))
    payload = json.dumps(edges).replace('<', '\\u003c')
    return '''<!doctype html><html><head><meta charset="utf-8"><style>
html,body{max-width:100%;overflow-x:hidden}body{margin:0;background:#0e1523;color:#e6edf7;font:14px system-ui}button,select{background:#243752;color:inherit;border:1px solid #7185a3;border-radius:6px;padding:7px;cursor:pointer}header{padding:12px;position:sticky;top:0;background:#0e1523;z-index:4}header p{color:#b0bfd3;font-size:12px;margin-bottom:0}.viewport{height:690px;overflow-y:auto;overflow-x:hidden;border:1px solid #40536e}.canvas{position:relative;width:100%;max-width:100%;padding:35px 18px;box-sizing:border-box}.rank{display:grid;grid-template-columns:minmax(0,1fr);gap:32px;max-width:920px;margin:0 auto 190px;position:relative;z-index:2}.node{width:100%;min-width:0;max-width:100%;box-sizing:border-box;border:1px solid #6485b4;border-radius:10px;background:#17263d;box-shadow:0 4px 14px #0004}.node[open]{width:100%}summary{padding:14px;cursor:pointer;font-weight:600}small{display:block;font-size:11px;font-weight:400;color:#acc5e6;margin-top:6px}.body{padding:0 16px 16px;overflow-wrap:anywhere}.prose{white-space:pre-wrap;line-height:1.5}.reason{border-left:3px solid #7dcbd3;padding:8px;background:#213950}table{border-collapse:collapse;font-size:12px}td,th{padding:7px;border:1px solid #57708d;text-align:left;white-space:nowrap}.table{max-width:100%;overflow-x:auto}img{max-width:100%}figure{margin:10px 0}.diagram-section{margin:28px 0 8px;padding:9px 11px;border-left:4px solid #7dcbd3;background:#213950;color:#e6edf7}pre{white-space:pre-wrap;font-size:11px}svg{position:absolute;inset:0;width:100%;height:100%;pointer-events:none;overflow:hidden;z-index:1}.edge-label{background:#0e1523;color:#b7d8e9;border:1px solid #40536e;border-radius:5px;padding:5px;font-size:11px;line-height:1.3;pointer-events:auto;max-height:78px;overflow:auto} .node:focus-within{outline:2px solid #b6e6fc}
header{display:flex;flex-wrap:wrap;gap:8px;box-sizing:border-box}header label{display:flex;flex-wrap:wrap;align-items:center;gap:6px;min-width:0;max-width:100%}header select{min-width:0;max-width:100%;width:320px;box-sizing:border-box}header p{flex-basis:100%}summary{overflow-wrap:anywhere}.table{overflow-x:visible}table{width:100%;table-layout:fixed}td,th{white-space:normal;overflow-wrap:anywhere}.edge-label{box-sizing:border-box;overflow-wrap:anywhere;overflow-x:hidden}
</style></head><body><header><button id="collapse">Collapse all</button> <button id="expand">Expand all</button> <label>Find experiment <select id="jump"><option value="">Choose…</option>''' + ''.join('<option value="n' + str(i) + '">' + esc(r.get('name', '')) + '</option>' for i, r in enumerate(records)) + '''</select></label><p>Solid arrows explain documented follow-ups. Dashed arrows show other recorded references, not established causal dependencies. Unlinked nodes have no recorded predecessor. Scroll to explore; expand a node for its full results, diagrams and provenance.</p></header><div class="viewport"><div class="canvas"><svg><defs><marker id="arrow" markerWidth="10" markerHeight="10" refX="9" refY="3" orient="auto"><path d="M0,0 L0,6 L9,3 z" fill="#83b7cc"/></marker></defs><g id="edges"></g></svg>''' + ranks + '''</div></div><script>
const edges = ''' + payload + ''';
const canvas=document.querySelector('.canvas'), layer=document.querySelector('#edges');
function draw(){layer.replaceChildren();const base=canvas.getBoundingClientRect();edges.forEach((e,index)=>{const a=document.getElementById('n'+e.source).getBoundingClientRect(),b=document.getElementById('n'+e.target).getBoundingClientRect();let x=a.left+a.width/2-base.left,y=a.bottom-base.top,u=b.left+b.width/2-base.left,v=b.top-base.top;const mid=y+80+(index%3)*24;let path=document.createElementNS('http://www.w3.org/2000/svg','path');path.setAttribute('d',`M ${x} ${y} C ${x} ${mid}, ${u} ${mid}, ${u} ${v}`);path.setAttribute('stroke','#83b7cc');if(e.kind==='Recorded reference')path.setAttribute('stroke-dasharray','5 5');path.setAttribute('fill','none');path.setAttribute('marker-end','url(#arrow)');layer.append(path);let label=document.createElementNS('http://www.w3.org/2000/svg','foreignObject');const labelWidth=Math.min(250,Math.max(150,base.width-24));const labelX=Math.max(12,Math.min((x+u)/2-labelWidth/2,base.width-labelWidth-12));label.setAttribute('x',labelX);label.setAttribute('y',mid-35);label.setAttribute('width',labelWidth);label.setAttribute('height',95);let box=document.createElement('div');box.className='edge-label';box.textContent=e.reason;box.title=e.kind+': '+e.reason;label.append(box);layer.append(label);});}
let pending=false;function schedule(){if(!pending){pending=true;requestAnimationFrame(()=>{pending=false;draw()})}}
new ResizeObserver(schedule).observe(canvas);document.querySelectorAll('.node').forEach(n=>n.addEventListener('toggle',schedule));document.getElementById('collapse').onclick=()=>document.querySelectorAll('.node').forEach(n=>n.open=false);document.getElementById('expand').onclick=()=>document.querySelectorAll('.node').forEach(n=>n.open=true);document.getElementById('jump').onchange=e=>{let n=document.getElementById(e.target.value);if(n){n.open=true;n.scrollIntoView({behavior:'smooth',block:'center',inline:'nearest'})}};schedule();
</script></body></html>'''
