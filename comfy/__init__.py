from server import PromptServer
from aiohttp import web, WSMsgType
import json
import asyncio
import threading
import torch
from io import BytesIO
import numpy as np
from PIL import Image, ImageFilter, ImageOps, ImageSequence
from nodes import LoadImage

class WSCallsManager:
    ws = None
    def __init__(self, ws):
        self.calls = dict()
        self.call_id = 0
        self.ws = ws
    
    async def call(self, action, params, timeout=5):
        self.call_id += 1
        payload = {
            'call_id': self.call_id,
            'action': action,
            'params': params
        }
        call = self.calls[self.call_id] = asyncio.Future()
        
        await self.ws.send_str(json.dumps(payload))
        start_time = asyncio.get_event_loop().time()
        while (call.done() == False):
            if (asyncio.get_event_loop().time() - start_time > timeout):
                call.cancel()
                raise ValueError('call timed out')
            await asyncio.sleep(0.1)    
        return call.result()
    
    def handle_call(self, call_id, result=None, error=None):
        call = self._get_call_and_remove(call_id)
        if (call.cancelled()):
            return
        
        if (error is not None):
            call.set_exception(ValueError(error))
        else:
            call.set_result(result)
    
    def _get_call_and_remove(self, call_id):
        call = self.calls.get(call_id, None)
        if call is not None:
            self.calls.pop(call_id)
        else:
            raise ValueError(f'call {call_id} not found')
        return call
    

class PhotoshopInstance:
    instance = None
    
    def __init__(self, ws):
        PhotoshopInstance.instance = self
        self.wsCallsManager = WSCallsManager(ws)
        self.destroyed = False
        self.layers = []
        
    async def poll_layers(self):
        await asyncio.sleep(1)
        while (self.destroyed == False and self.wsCallsManager.ws.closed == False):
            try:
                # print('start poll layers')
                self.layers = await self.get_layers()
                # print('layers done')
            except Exception as e:
                await asyncio.sleep(3)
            finally:
                await asyncio.sleep(1)

    async def run_server_loop(self):
        print('Photoshop Connected')
        
        threading.Thread(target=lambda: asyncio.run(self.poll_layers())).start()
        try:
            async for msg in self.wsCallsManager.ws:
                if (self.destroyed): break
                                                                                                     
                if msg.type == WSMsgType.TEXT:
                    payload = json.loads(msg.data)
                    
                    if 'call_id' in payload:
                        call_id = payload['call_id']
                        if 'error' in payload:
                            self.wsCallsManager.handle_call(call_id, error=payload['error'])
                        elif 'result' in payload:
                            self.wsCallsManager.handle_call(call_id, result=payload['result'])
                        else:
                            await self.wsCallsManager.ws.send_str(json.dumps({ call_id: call_id, 'error': 'result not found in payload'}))
                    else: 
                        await self.wsCallsManager.ws.send_str(json.dumps({ 'error': 'call_id not found in payload'}))
                        
                elif msg.type == WSMsgType.ERROR:
                    print('ws connection closed with exception %s' % self.wsCallsManager.ws.exception())
                    
                else:
                    await self.wsCallsManager.ws.send_str('invalid msg type')
        finally:
            print('Photoshop Disconnected')
            PhotoshopInstance.instance = None
            
    async def get_layers(self):
        result = await self.wsCallsManager.call('get_layers', {})
        layers = result['layers']                           
        return layers
        
    async def get_image(self, layer_id):
        result = await self.wsCallsManager.call('get_image', {'layer_id': layer_id}, timeout=60)
        id = result['upload_name']
        return id
    
    async def send_images(self, image_ids):
        result = await self.wsCallsManager.call('send_images', {'image_ids': image_ids})
        return result
            
    async def destroy(self):
        await self.wsCallsManager.ws.close()
        self.destroyed = True
    
class ImageCache:
    data = dict()
    image_id_inc = 0
    
@PromptServer.instance.routes.get('/finished_images')
async def download_handler(request):
    try:
        image_id = request.query.get('id')
        if (image_id is None):
            return web.json_response({
                'error': 'id is required'
            })
        
        image_id = int(image_id)
        if (image_id not in ImageCache.data):
            return web.json_response({
                'error': 'image not found'
            })
        
        image = ImageCache.data[image_id]
        ImageCache.data.pop(image_id)
        # image = image.tobytes()
        
        stream = BytesIO()
        image.save(stream, "PNG")
        
        return web.Response(body=stream.getvalue(), content_type='image/png')
    except Exception as e:
        print('=============error============', e)
        return web.json_response({
            'error': str(e)
        })
    
# @PromptServer.instance.routes.post('/photoshop_upload')
# async def upload_handler(request):
#     try:
#         start_time = asyncio.get_event_loop().time()
#         pixels = await request.read()
#         print('readed pixels in ' + str(asyncio.get_event_loop().time() - start_time) + ' s')
#         # get url param 'width'
#         width = int(request.query.get('width'))
#         if (width == 0 or width is None):
#             return web.json_response({
#                 'error': 'width is required'
#             })
        
        
#         pixels = [x for x in pixels]
#         arr = np.array(pixels, dtype=np.uint8)
#         arr = np.reshape(arr, (-1, width, 4))
#         image = Image.fromarray(arr)
#         # print(f'Parsed image width: {image.width}, height: {image.height} in {asyncio.get_event_loop().time() - start_time} ms')
        
#         image = image.convert("RGB")
#         image = np.array(image).astype(np.float32) / 255.0
#         image = torch.from_numpy(image)[None,]
#         # print(f'image to latent in {asyncio.get_event_loop().time() - start_time} ms')
        
#         global image_id_inc
#         image_id_inc += 1
#         ImageCache.data[image_id_inc] = image
        
#     except Exception as e:
#         return web.json_response({
#             'error': str(e)
#         })
    
#     return web.json_response({
#         'name': image_id_inc
#     })

@PromptServer.instance.routes.get('/photoshop_instance')
async def websocket_handler(request):
    # get version from query
    print('try connect: ' + str(request.query))
    version = int(request.query.get('version', 0))
    EXPECTED_VERSION = 1
    
    if (version is not EXPECTED_VERSION):
        if (version == 0):
            return web.json_response({ 
                'error': f'version is not provided.',
            })
        else:
            return web.json_response({ 
                'error': f'version {version} not supported.',
            })
        
    if (PhotoshopInstance.instance is not None):
        # return web.json_response({ 
        #     'error': 'only one instance of Photoshop is allowed at a time. Please close the current instance first.',
        # })
        print('destroying previous instance')
        await PhotoshopInstance.instance.destroy()
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    
    PhotoshopInstance(ws)
    await PhotoshopInstance.instance.run_server_loop()

class GetImageFromPhotoshopLayerNode:
    @classmethod
    def VALIDATE_INPUTS(s, layer):
        if (PhotoshopInstance.instance is None):
            return 'Photoshop is not connected'
        return True
    
    @classmethod
    def IS_CHANGED(s, layer):
        return np.random.rand()
    
    @classmethod
    def INPUT_TYPES(cls):
        if (PhotoshopInstance.instance is None):
            layer_strs = []
        else:
            layer_strs = list(map(lambda layer: 
                f"{layer['name']} (id:{layer['id']})"
            , PhotoshopInstance.instance.layers))
            
        return {
            "required": {
                "layer": (layer_strs, ),
            }
        }

    RETURN_TYPES = ("IMAGE",)
    RETURN_NAMES = ("image_out",)
    FUNCTION = "get_image"
    CATEGORY = "Photoshop"

    def get_image(self, layer):
        if (PhotoshopInstance.instance is None):
            raise ValueError('Photoshop is not connected')
        
        layer_name_and_id_split = layer.split('(id:')
        id = int(layer_name_and_id_split.pop().strip()[:-1])
        image_id = _invoke_async(PhotoshopInstance.instance.get_image(layer_id=id))
        
        loadImage = LoadImage()
        (output_image, output_mask) = loadImage.load_image(image_id)
        
        # image_out = ImageCache.data[id]
        # ImageCache.data.pop(id)
        
        return (output_image,)
    
class SendImageToPhotoshopLayerNode:
    @classmethod
    def VALIDATE_INPUTS(s, images):
        if (PhotoshopInstance.instance is None):
            return 'Photoshop is not connected'
        return True
    
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "images": ("IMAGE", )
            }
        }

    RETURN_TYPES = ()
    FUNCTION = "send_image"
    CATEGORY = "Photoshop"
    OUTPUT_NODE = True

    def send_image(self, images):
        if (PhotoshopInstance.instance is None):
            raise ValueError('Photoshop is not connected')
        
        ret = []
        for (batch_number, image) in enumerate(images):
            i = 255. * image.cpu().numpy()
            img = Image.fromarray(np.clip(i, 0, 255).astype(np.uint8))
            ImageCache.image_id_inc += 1
            image_id = ImageCache.image_id_inc
            ImageCache.data[image_id] = img
            ret.append(image_id)
        
        threading.Thread(target=lambda: asyncio.run(PhotoshopInstance.instance.send_images(image_ids=ret))).start()
        return (None,)

loop = None
def _invoke_async(call):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    task = loop.create_task(call)
    loop.run_until_complete(task)
    result = task.result()
    loop.close()
    return result

NODE_CLASS_MAPPINGS = { 
    'Get Image From Photoshop Layer': GetImageFromPhotoshopLayerNode,
    'Send Images To Photoshop': SendImageToPhotoshopLayerNode 
}
NODE_DISPLAY_NAME_MAPPINGS = { 
    'Get Image From Photoshop Layer': 'Get image from Photoshop layer',
    'Send Images To Photoshop': 'Send images to Photoshop' 
}
WEB_DIRECTORY = 'comfy/plugins'
__all__ = ['NODE_CLASS_MAPPINGS', 'NODE_DISPLAY_NAME_MAPPINGS', 'WEB_DIRECTORY']