"""Standalone product server: never initializes the research ReviewStore or Pool."""
from pathlib import Path
from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from .product_api import router

ROOT=Path(__file__).resolve().parents[1]
app=FastAPI(title='认知证据工作台',docs_url=None,redoc_url=None)
app.include_router(router)


@app.middleware('http')
async def no_cache(request,call_next):
    response=await call_next(request)
    if request.url.path.startswith('/api/'):
        response.headers['Cache-Control']='no-store'
    return response


@app.exception_handler(Exception)
async def internal_error(request,error):
    # Provider exceptions may embed URLs or credentials: never reflect them to a browser.
    return JSONResponse(status_code=500,content={'detail':'服务处理失败，请检查产品运行日志与来源校验'})


if (ROOT/'demo/dist/assets').exists():
    app.mount('/assets',StaticFiles(directory=ROOT/'demo/dist/assets'),name='assets')


@app.get('/')
def home(): return FileResponse(ROOT/'demo/dist/index.html',headers={'Cache-Control':'no-cache'})


@app.get('/health')
def health(): return {'ok':True,'service':'e-workbench'}


@app.get('/api/demo')
def synthetic_experiments():
    from .analysis import redteam, causal_simulation
    from .legacy_workbench import clean
    return clean(dict(redteam=redteam().to_dict('records'),simulation=causal_simulation().to_dict('records'),source='synthetic_constructed_labels'))
