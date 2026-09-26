"""Start the isolated synthetic workbench on a loopback interface."""
import argparse
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))
os.environ['WORKBENCH_RESEARCH_ROOT'] = str(ROOT / 'research')
os.environ['WORKBENCH_CREDENTIALS_FILE'] = str(ROOT / 'research' / '.env-unused')
os.environ['WORKBENCH_GATE_FILE'] = str(ROOT / 'research' / '.gate-unused')
os.environ.pop('WORKBENCH_TOKEN', None)

from aiv.product_server import app
from fastapi.responses import JSONResponse

@app.middleware('http')
async def synthetic_only(request, call_next):
    # No authenticated imports or live jobs in the public demonstration.
    if request.headers.get('x-workbench-token'):
        return JSONResponse({'detail': 'This launcher serves synthetic replay.'}, status_code=403)
    return await call_next(request)

def no_outbound(event, args):
    if event == 'socket.connect':
        address = args[1]
        # Windows asyncio uses a local socket pair for its event loop.
        if not isinstance(address, tuple) or address[0] not in ('127.0.0.1', '::1', 'localhost'):
            raise PermissionError('External connections are disabled in the synthetic launcher')

if __name__ == '__main__':
    import uvicorn
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8780)
    args = parser.parse_args()
    sys.addaudithook(no_outbound)
    uvicorn.run(app, host='127.0.0.1', port=args.port, workers=1)
