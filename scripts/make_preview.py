"""Turn a Portfolio Lab page into its preview copy at /portfolio-lab-preview/.

    python scripts/make_preview.py SOURCE_PAGE OUTPUT_PAGE "label"

The preview reads the live site's data (../portfolio-lab/data/), keeps its own saved settings
(copied once from the live page, so trying things there never changes the real page), stays out
of search results, and shows a small "Preview" tag so it can't be mistaken for the live page.
"""
import sys

src, out, label = sys.argv[1], sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else ""
page = open(src, encoding="utf-8").read()

page = page.replace("'data/", "'../portfolio-lab/data/")
page = page.replace("'pl:'+", "'plpv:'+").replace("'pl:theme'", "'plpv:theme'")

head = """<meta name="robots" content="noindex">
<script>/* preview: start from a copy of the live page's saved settings, then keep them separate */
try{if(!localStorage.getItem('plpv:_copied')){const ks=[];for(let i=0;i<localStorage.length;i++){const k=localStorage.key(i);if(k&&k.indexOf('pl:')===0)ks.push(k)}
ks.forEach(k=>localStorage.setItem('plpv:'+k.slice(3),localStorage.getItem(k)));localStorage.setItem('plpv:_copied','1')}}catch(e){}</script>
"""
tag = ('<div style="position:fixed;left:12px;bottom:12px;z-index:9999;padding:6px 10px;border-radius:8px;'
       'background:#c2410c;color:#fff;font:600 12px/1.2 system-ui,sans-serif;box-shadow:0 2px 8px rgba(0,0,0,.3);'
       'pointer-events:none">Preview' + (" &middot; " + label.replace("<", "&lt;") if label else "") + "</div>\n")

# the page has no <head>/<body> tags: the extras go right after the charset line and at the very end
anchor = '<meta charset="utf-8">'
assert anchor in page, "page layout changed; update make_preview.py"
page = page.replace(anchor, anchor + "\n" + head, 1).rstrip() + "\n" + tag
open(out, "w", encoding="utf-8").write(page)
