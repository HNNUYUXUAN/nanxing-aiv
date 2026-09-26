import {fileURLToPath,pathToFileURL} from 'node:url';
import path from 'node:path';
const toolchain=process.env.WORKBENCH_TOOLCHAIN_ROOT || fileURLToPath(new URL('../tools/web-toolchain',import.meta.url));
const dependency=name=>path.join(toolchain,'node_modules',name);
const {defineConfig}=await import(pathToFileURL(dependency('vite/dist/node/index.js')).href);
const {default:react}=await import(pathToFileURL(dependency('@vitejs/plugin-react/dist/index.js')).href);
export default defineConfig({plugins:[react()],resolve:{alias:{'react-dom':dependency('react-dom'),'react':dependency('react'),'echarts':dependency('echarts'),'lucide-react':dependency('lucide-react')}},server:{host:'127.0.0.1',proxy:{'/api':'http://127.0.0.1:8770'}},build:{outDir:'dist'}});
