"""Fill template.html with pagedata.json (live traces + prompt) -> index.html."""
import json, os
here = os.path.dirname(os.path.abspath(__file__))
data = json.dumps(json.load(open(os.path.join(here, "pagedata.json")))).replace("</", "<\\/")
html = open(os.path.join(here, "template.html")).read().replace("__DATA__", data)
open(os.path.join(here, "index.html"), "w").write(html)
print("index.html", len(html), "bytes")
