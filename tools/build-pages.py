"""Build the Pages copy from tracked, licensed frontend assets only."""
from pathlib import Path
import json,re,subprocess,posixpath
root=Path(__file__).resolve().parents[1]
files=subprocess.check_output(['git','ls-files','static'],cwd=root,text=True).splitlines()
assets={p.removeprefix('static/') for p in files}
for name in files:
 rel=name.removeprefix('static/')
 if rel in {'sw.js','manifest.json'}:continue
 dest=root/'docs'/rel;dest.parent.mkdir(parents=True,exist_ok=True)
 data=(root/name).read_bytes()
 if dest.suffix in {'.html','.js','.css'}:
  s=data.decode()
  # Root-relative frontend resources become relative to the containing file.
  for asset in sorted(assets,key=len,reverse=True):
   target=posixpath.relpath(asset,posixpath.dirname(rel) or '.')
   s=re.sub(r'([\'"`(])/'+re.escape(asset)+r'(?=[?\'"`)#])',lambda m:m[1]+target,s)
  s=s.replace('/icon-192.png','icon.svg')
  if rel=='index.html':
   s=s.replace('return attUrl(u + "/download");','return attUrl(u);')
   s=s.replace('new URL("/previews/catalogue.json", location.href)','new URL("previews/catalogue.json", location.href)')
  if dest.suffix=='.html':
   s=re.sub(r'<link[^>]+rel="manifest"[^>]*>','',s)
   s=re.sub(r'navigator\.serviceWorker\.register\([^)]*\)', 'Promise.resolve(null)',s)
   if rel in {'study.html','study-note.html'}:s=s.replace('href="/"','href="index.html"')
   if rel in {'index.html','study.html','study-note.html'}:
    s=s.replace('<head>',"<head>\n<meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'self' data: blob:; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; font-src 'self' data:; connect-src 'self'; worker-src 'none'; object-src 'none'\">\n<script src=\"pages-replay.js\"></script>",1)
   if rel in {'index.html','desktop-frame.html'}:
    s=s.replace('预设剧本回放演示</', '预设剧本回放演示，不连接任何模型或服务器</')
  data=s.encode()
 dest.write_bytes(data)
scenes=json.loads((root/'backend/scenes.json').read_text())
source=(root/'tools/pages-replay.js').read_text().replace('[]; // SCENES',json.dumps(scenes,ensure_ascii=False)+';')
(root/'docs/pages-replay.js').write_text(source)
(root/'docs/.nojekyll').touch()
