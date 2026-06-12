bl_info = {
    "name": "LLM4Blender",
    "author": "tin2tin",
    "version": (3, 9, 35),
    "blender": (5, 2, 0),
    "location": "Text Editor > Sidebar > LLM4Blender",
    "description": "Screenwriting, Coding & Image Prompts Workflow",
    "warning": "Requires Ollama installed",
    "category": "Development",
}

import bpy
import requests
import json
import threading
import time
import os
import random
import aud
import re
import hashlib
from bpy.props import StringProperty, BoolProperty, EnumProperty, CollectionProperty, PointerProperty, IntProperty, FloatProperty
from bpy.types import Operator, Panel, PropertyGroup

# --- CONSTANTS ---

OLLAMA_API_BASE = "http://localhost:11434/api"

# --- GLOBAL STATE ---

MODELS_CACHE = [("NONE", "Click Refresh to load", "")]
TEMPLATES_CACHE = [("PYTHON", "Python Coder", "")]
TEMPLATES_DIR_STATE = {}  # filename -> mtime, used by folder watcher
SERVER_ONLINE = False

# --- UTILITIES (LOGGING) ---

def log_msg(msg):
    print(f"[LLM4Blender] {msg}")

# --- TEMPLATES ---

TEMPLATE_DISPLAY_NAMES = {
    'PYTHON': 'Python Coder',
    'ADDON': 'Addon Developer',
    'FOUNTAIN': 'Screenwriter',
    'DIALOGUE': 'Dialogue Writer',
    'CHARACTER': 'Character Development',
    'TREATMENT': 'Story Treatment',
    'IMAGE': 'Convert to Image Prompts',
    'IDEOGRAM4': 'Ideogram 4 JSON',
    'CINEMASCOPE_PROMPT': 'Cinemascope Prompt',
    'LTX_VIDEO': 'LTX 2.3 Video Prompt',
    'SCREENPLAY_SHOTS': 'Insert Shots into Screenplay',
    'CINEMATIC_PROSE': 'Cinematic Prose Style',
    'TTS_CLEANUP': 'TTS Cleanup',
    'YOUTUBE_CONCEPT': 'YouTube: Concept',
    'YOUTUBE_SCRIPT': 'YouTube: Script',
    'YOUTUBE_SHOTS': 'YouTube: Shot List',
    'YOUTUBE_BROLL': 'YouTube: B-Roll',
    'YOUTUBE_EDIT': 'YouTube: Edit Plan',
    'YOUTUBE_SOUND': 'YouTube: Sound Design',
    'YOUTUBE_AUDIT': 'YouTube: Retention Audit',
}

def get_templates_dir():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")

def refresh_templates_cache():
    global TEMPLATES_CACHE
    tdir = get_templates_dir()
    items = []
    if os.path.isdir(tdir):
        for f in sorted(os.listdir(tdir)):
            if f.lower().endswith('.txt'):
                key = f[:-4]
                display = TEMPLATE_DISPLAY_NAMES.get(key, key.replace('_', ' ').title())
                items.append((key, display, ''))
    TEMPLATES_CACHE = items if items else [("PYTHON", "Python Coder", "")]

def get_template_items(self, context):
    return TEMPLATES_CACHE

def get_system_message(template_key):
    filepath = os.path.join(get_templates_dir(), template_key + ".txt")
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            return f.read()
    except Exception as e:
        log_msg(f"Warning: Could not load template '{template_key}': {e}")
        return ""

def _watch_templates_dir():
    global TEMPLATES_DIR_STATE
    tdir = get_templates_dir()
    if not os.path.isdir(tdir):
        return 3.0
    current = {}
    for f in os.listdir(tdir):
        if f.lower().endswith('.txt'):
            try:
                current[f] = os.path.getmtime(os.path.join(tdir, f))
            except OSError:
                pass
    if current != TEMPLATES_DIR_STATE:
        TEMPLATES_DIR_STATE = current
        refresh_templates_cache()
        for window in bpy.context.window_manager.windows:
            for area in window.screen.areas:
                area.tag_redraw()
    return 3.0

# --- UTILITIES ---

def get_download_path():
    """Returns the path: C:/Users/USER/.ollama/imported_ggufs"""
    user_home = os.path.expanduser("~")
    path = os.path.join(user_home, ".ollama", "imported_ggufs")
    if not os.path.exists(path):
        os.makedirs(path)
    return path

def check_server_status_quick():
    """Checks if Ollama is running (blocking, use sparsely). updates GLOBAL."""
    global SERVER_ONLINE
    try:
        requests.get(f"{OLLAMA_API_BASE}/version", timeout=0.2)
        SERVER_ONLINE = True
        return True
    except:
        SERVER_ONLINE = False
        return False

def update_model_cache():
    global MODELS_CACHE, SERVER_ONLINE
    log_msg("Refreshing model list & server status...")
    items = []
    
    # 1. API Models
    try:
        response = requests.get(OLLAMA_API_BASE + "/tags", timeout=1)
        if response.status_code == 200:
            SERVER_ONLINE = True
            models = response.json().get('models', [])
            for m in models:
                name = m['name']
                display = name.split(':')[0]
                items.append((name, display, f"Installed: {name}"))
        else:
             SERVER_ONLINE = False
             items.append(("NONE", "Ollama Error (See Console)", ""))
    except:
        SERVER_ONLINE = False
        log_msg("Warning: Ollama API unreachable.")
        items.append(("NONE", "OFFLINE: Start Ollama App", ""))

    # 2. Local Files
    dl_path = get_download_path()
    if os.path.exists(dl_path):
        try:
            for f in os.listdir(dl_path):
                if f.lower().endswith(".gguf"):
                    items.append((f, f"📄 {f}", f"Local File: {os.path.join(dl_path, f)}"))
        except: pass

    if items:
        if items[0][0] == "NONE":
            pass 
        else:
            items.sort(key=lambda x: x[1])
        MODELS_CACHE = items
    else:
        MODELS_CACHE = [("NONE", "No Models Found", "")]

def get_cached_models(self, context):
    return MODELS_CACHE

def calculate_sha256(filepath, status_callback=None):
    """Calculates SHA256 of a file with progress updates."""
    sha256_hash = hashlib.sha256()
    file_size = os.path.getsize(filepath)
    processed = 0
    
    with open(filepath, "rb") as f:
        # 1MB chunks
        for byte_block in iter(lambda: f.read(1048576), b""):
            sha256_hash.update(byte_block)
            processed += len(byte_block)
            if status_callback:
                pct = processed / file_size
                status_callback(f"Hashing GGUF...", pct)
                
    return sha256_hash.hexdigest()

def ensure_model_ready(model_identifier, report_error=None, status_updater=None):
    """
    Registers a local .gguf file with Ollama by uploading it as a BLOB
    and mapping it via 'files' in the create payload.
    """
    if not check_server_status_quick():
        err = "Ollama is not running. Please start the Ollama app."
        log_msg(err)
        if report_error: report_error(err)
        return None

    if not model_identifier.lower().endswith(".gguf"):
        return model_identifier 
    
    filename = model_identifier
    dl_path = get_download_path()
    file_path = os.path.join(dl_path, filename)
    
    if not os.path.exists(file_path):
        err = f"File not found: {file_path}"
        log_msg(err)
        if report_error: report_error(err)
        return None

    clean_name = filename.replace(".gguf", "").lower()
    clean_name = re.sub(r'[^a-z0-9]', '_', clean_name)
    
    # --- STEP 1: Calculate Digest ---
    log_msg(f"Calculating SHA256 for {filename}...")
    if status_updater: status_updater("Hashing...", 0.1)
    
    digest_clean = calculate_sha256(file_path, status_updater)
    digest = f"sha256:{digest_clean}"
    
    # --- STEP 2: Upload Blob ---
    blob_url = f"{OLLAMA_API_BASE}/blobs/{digest}"
    
    # Check if exists first
    need_upload = True
    try:
        if requests.head(blob_url).status_code == 200:
            need_upload = False
            log_msg("Blob already exists in Ollama.")
            if status_updater: status_updater("Blob Checked", 0.8)
    except: pass
    
    if need_upload:
        log_msg("Uploading Blob to Ollama...")
        file_size = os.path.getsize(file_path)
        
        # Helper to stream upload with progress
        def file_reader_with_progress():
            processed = 0
            with open(file_path, 'rb') as f:
                while True:
                    chunk = f.read(1048576)
                    if not chunk: break
                    processed += len(chunk)
                    if status_updater:
                        status_updater("Uploading Blob...", processed / file_size)
                    yield chunk

        try:
            r = requests.post(blob_url, data=file_reader_with_progress(), timeout=None)
            if r.status_code not in [200, 201]:
                err = f"Blob Upload Failed: {r.status_code}"
                if report_error: report_error(err)
                return None
        except Exception as e:
            if report_error: report_error(f"Upload Error: {e}")
            return None

    # --- STEP 3: Create Model using 'files' mapping ---
    log_msg("Registering Model...")
    if status_updater: status_updater("Registering...", 0.95)
    
    # We map the filename to the digest. 
    # This solves "neither 'from' or 'files' specified" error.
    
    modelfile = f"""
FROM {filename}
TEMPLATE \"\"\"{{{{ .Prompt }}}}\"\"\"
"""
    
    payload = {
        "name": clean_name,
        "modelfile": modelfile,
        "files": {
            filename: digest  # MAPPING: "my.gguf" -> "sha256:..."
        },
        "stream": False
    }
    
    try:
        r = requests.post(f"{OLLAMA_API_BASE}/create", json=payload, timeout=120)
        if r.status_code != 200:
            err = f"Registration Error: {r.text}"
            log_msg(err)
            if report_error: report_error(err)
            return None
    except Exception as e:
        if report_error: report_error(f"Create Error: {e}")
        return None

    log_msg(f"Success! Model registered as '{clean_name}'")
    return clean_name

def play_notification_sound(context):
    props = context.scene.llm4blender_props
    if not props.play_sound: return
    try:
        device = aud.Device()
        if props.sound_select == "ding":
            sound = aud.Sound("")
            device.play(sound.sine(1000).ADSR(0.01, 0.1, 0, 0).limit(0, 0.2))
        elif props.sound_select == "coin":
            sound = aud.Sound("")
            device.play(sound.sine(1000).ADSR(0, 0.1, 0, 0).limit(0, 0.1))
            device.play(sound.sine(1500).ADSR(0, 0.1, 0, 0).delay(0.1).limit(0, 0.1))
        elif props.sound_select == "user" and os.path.isfile(props.user_sound_path):
            sound = aud.Sound(props.user_sound_path)
            device.play(sound)
    except: pass

class ChatHistoryItem(PropertyGroup):
    input: StringProperty()
    output: StringProperty()

# --- OPERATORS ---

class LLM4BLENDER_OT_SessionControl(bpy.types.Operator):
    bl_idname = "llm4blender.session_control"
    bl_label = "Session Control"
    action: EnumProperty(items=[('START', "Start", ""), ('STOP', "Stop", "")])

    def execute(self, context):
        props = context.scene.llm4blender_props
        raw_model = props.model_selector
        
        if not check_server_status_quick():
            self.report({'ERROR'}, "Ollama is not running.")
            return {'CANCELLED'}

        if raw_model in ["NONE", "OFFLINE"]: 
            self.report({'ERROR'}, "Please select a valid model first.")
            return {'CANCELLED'}
        
        if self.action == 'START':
            self.report({'INFO'}, f"LLM: Initializing {raw_model}...")
            props.is_session_active = True 
            threading.Thread(target=self.run_session_start, args=(raw_model,)).start()
        else:
            self.report({'INFO'}, f"LLM: Unloading...")
            props.is_session_active = False
            threading.Thread(target=self.run_session_stop).start()
            
        return {'FINISHED'}

    def run_session_start(self, raw_model):
        model = ensure_model_ready(raw_model)
        if model:
            try: requests.post(f"{OLLAMA_API_BASE}/chat", json={"model": model, "messages": [], "keep_alive": -1}, timeout=10)
            except: pass

    def run_session_stop(self):
         try: requests.post(f"{OLLAMA_API_BASE}/generate", json={"keep_alive": 0}, timeout=5)
         except: pass

class LLM4BLENDER_OT_Generate(bpy.types.Operator):
    bl_idname = "llm4blender.generate"
    bl_label = "Generate"
    
    mode: StringProperty(default="CHAT")
    _timer = None
    _thread = None
    _response = None
    _is_running = False
    _input_text = ""
    _error_msg = None
    _current_status_text = ""
    _current_progress = 0.0

    def modal(self, context, event):
        if event.type == 'TIMER':
            # Update UI from thread
            props = context.scene.llm4blender_props
            props.status_message = self._current_status_text
            props.progress = self._current_progress
            
            if self._response is not None or self._error_msg is not None:
                try:
                    if self._error_msg:
                        self.report({'ERROR'}, self._error_msg)
                        log_msg(self._error_msg)
                    elif self._response:
                        self.finish_processing(context, self._response)
                except Exception as e:
                    self.report({'ERROR'}, f"Writing Error: {e}")
                finally:
                    self._response = None
                    self._error_msg = None
                    self._is_running = False
                    props.progress = 0.0
                    props.status_message = "Ready"
                    context.workspace.status_text_set(None)
                    if self._timer:
                        context.window_manager.event_timer_remove(self._timer)
                        self._timer = None
                    context.region.tag_redraw() 
                    return {'FINISHED'}
            
            # Simple animation
            if "Generating" in self._current_status_text:
                dots = "." * (int(time.time()) % 4)
                context.workspace.status_text_set(f"LLM: Generating{dots}")
            
            context.region.tag_redraw()
            
        return {'PASS_THROUGH'}

    def execute(self, context):
        if self._is_running: return {'CANCELLED'}
        props = context.scene.llm4blender_props
        
        if not check_server_status_quick():
            self.report({'ERROR'}, "Ollama is not running. Start the app.")
            return {'CANCELLED'}

        if self.mode == 'REWRITE':
            text = context.space_data.text
            if not text:
                self.report({'ERROR'}, "No active text file.")
                return {'CANCELLED'}
            content = text.as_string().strip()
            if not content: return {'CANCELLED'}

            if props.template_type in ['IMAGE', 'IDEOGRAM4', 'CINEMASCOPE_PROMPT', 'LTX_VIDEO', 'SCREENPLAY_SHOTS']:
                prompt = f"Here is the content:\n\n{content}"
                self._input_text = f"Process ({props.template_type})"
            else:
                prompt = f"{props.rewrite_prefix}:\n\n{content}"
                self._input_text = f"{props.rewrite_prefix} (File)"
        else:
            prompt = props.prompt_input.strip()
            if not prompt: return {'CANCELLED'}
            self._input_text = prompt

        system_msg = get_system_message(props.template_type)
        messages = [{"role": "system", "content": system_msg}]

        if self.mode == 'CHAT':
            recent = list(props.chat_history)[-3:]
            for item in recent:
                messages.append({"role": "user", "content": item.input})
                messages.append({"role": "assistant", "content": item.output})
        
        messages.append({"role": "user", "content": prompt})

        final_seed = random.randint(0, 9999999) if props.use_random_seed else props.seed
        options = {"temperature": props.temperature, "seed": final_seed, "num_predict": -1, "num_ctx": 8192}

        raw_model = props.model_selector
        
        self._is_running = True
        self._response = None
        self._error_msg = None
        self._current_status_text = "Initializing..."
        self._current_progress = 0.0
        
        if not props.is_session_active: props.is_session_active = True
        
        self._thread = threading.Thread(
            target=self.run_generation, 
            args=(raw_model, messages, options)
        )
        self._thread.start()
        
        self._timer = context.window_manager.event_timer_add(0.5, window=context.window)
        context.window_manager.modal_handler_add(self)
        return {'RUNNING_MODAL'}

    def run_generation(self, raw_model, messages, options):
        def set_err(msg): self._error_msg = msg
        def update_status(txt, pct): 
            self._current_status_text = txt
            self._current_progress = pct
        
        # Ensures model is imported (hashed/uploaded) if needed
        model = ensure_model_ready(raw_model, report_error=set_err, status_updater=update_status)
        
        if not model:
            if not self._error_msg: self._error_msg = "Unknown error registering model."
            return

        try:
            update_status("Generating response...", 1.0) # Reset bar for generation
            # Brief pause so bar looks full
            time.sleep(0.1)
            update_status("Generating response...", 0.0) 

            payload = {
                "model": model, 
                "messages": messages, 
                "stream": False, 
                "keep_alive": -1,
                "options": options
            }
            log_msg(f"Sending request to {model}...")
            r = requests.post(f"{OLLAMA_API_BASE}/chat", json=payload, timeout=3600)
            
            if r.status_code == 200: 
                content = r.json().get("message", {}).get("content", "")
                if not content:
                    self._response = "# Warning: Model returned empty response."
                else:
                    self._response = content
            else: 
                self._error_msg = f"API Error {r.status_code}: {r.text}"
        except Exception as e: 
            self._error_msg = f"Connection Error: {e}"

    def finish_processing(self, context, result_text):
        props = context.scene.llm4blender_props

        item = props.chat_history.add()
        item.input = self._input_text
        item.output = result_text

        text_data = context.space_data.text or bpy.data.texts.new("LLM_Generated.txt")
        context.space_data.text = text_data

        if len(text_data.as_string()) < 5:
            text_data.write(result_text)
        else:
            text_data.write("\n" + "="*30 + "\n" + result_text + "\n")

        self.report({'INFO'}, "Generation Complete!")
        play_notification_sound(context)

# --- IMPORT OPERATOR ---

class LLM4BLENDER_OT_ImportGGUF(bpy.types.Operator):
    bl_idname = "llm4blender.import_gguf"
    bl_label = "Import GGUF"
    
    _timer = None
    _thread = None
    _status = ""
    _is_done = False
    _error = None
    _progress = 0.0

    def modal(self, context, event):
        if event.type == 'TIMER':
            # Sync UI
            props = context.scene.llm4blender_props
            props.status_message = self._status
            props.progress = self._progress

            if self._error: 
                self.report({'ERROR'}, self._error)
                props.status_message = "Error"
                props.progress = 0.0
                return {'CANCELLED'}
            
            if self._is_done:
                update_model_cache()
                context.region.tag_redraw()
                play_notification_sound(context)
                self.report({'INFO'}, "Model Downloaded & Installed!")
                props.status_message = "Ready"
                props.progress = 0.0
                return {'FINISHED'}
            
            context.workspace.status_text_set(f"LLM: {self._status}")
            context.region.tag_redraw()
        return {'PASS_THROUGH'}

    def execute(self, context):
        props = context.scene.llm4blender_props
        
        if not check_server_status_quick():
            self.report({'ERROR'}, "Ollama is not running.")
            return {'CANCELLED'}

        url = props.custom_url.strip(); name = props.custom_name.strip()
        if not url or not name: return {'CANCELLED'}
        
        self._is_done = False
        self._error = None
        self._status = "Starting..."
        self._progress = 0.0
        
        self._thread = threading.Thread(target=self.run_import, args=(url, name))
        self._thread.start()
        
        self._timer = context.window_manager.event_timer_add(0.2, window=context.window)
        context.window_manager.modal_handler_add(self)
        return {'RUNNING_MODAL'}
    
    def run_import(self, url, name):
        try:
            # 1. Download
            self._status = "Connecting..."
            if not name.lower().endswith(".gguf"): name += ".gguf"
            
            dl_path = get_download_path()
            file_path = os.path.join(dl_path, name)
            
            if not os.path.exists(file_path):
                with requests.get(url, stream=True, timeout=10) as r:
                    r.raise_for_status()
                    total_length = r.headers.get('content-length')
                    
                    if total_length is None: 
                        with open(file_path, 'wb') as f:
                            self._status = "Downloading (Unknown size)..."
                            for chunk in r.iter_content(chunk_size=8192):
                                if chunk: f.write(chunk)
                    else:
                        dl = 0
                        total_length = int(total_length)
                        with open(file_path, 'wb') as f:
                            for chunk in r.iter_content(chunk_size=8192):
                                if chunk:
                                    dl += len(chunk)
                                    f.write(chunk)
                                    pct = int(dl / total_length * 100)
                                    self._progress = dl / total_length
                                    self._status = f"Downloading: {pct}%"
            
            # 2. Register via Blob method (Updates self._status/progress)
            def update_ui_wrapper(txt, pct):
                self._status = txt
                self._progress = pct

            res = ensure_model_ready(name, status_updater=update_ui_wrapper)
            
            if not res:
                self._error = "Download successful, but import failed."
            else:
                self._is_done = True
                
        except Exception as e:
            self._error = str(e)

# --- UI CLASSES ---

class LLM4BLENDER_OT_RemoveHistory(bpy.types.Operator):
    bl_idname = "llm4blender.remove_history"
    bl_label = "Remove"
    index: IntProperty()
    def execute(self, context):
        context.scene.llm4blender_props.chat_history.remove(self.index); return {'FINISHED'}

class LLM4BLENDER_OT_CopyHistory(bpy.types.Operator):
    bl_idname = "llm4blender.copy_history"
    bl_label = "Copy"
    index: IntProperty()
    def execute(self, context):
        item = context.scene.llm4blender_props.chat_history[self.index]
        context.window_manager.clipboard = f"# PROMPT:\n{item.input}\n\n# RESULT:\n{item.output}"
        self.report({'INFO'}, "Copied to Clipboard")
        return {'FINISHED'}

class LLM4BLENDER_OT_RefreshModels(bpy.types.Operator):
    bl_idname = "llm4blender.refresh_models"
    bl_label = "Refresh"
    def execute(self, context):
        update_model_cache(); context.region.tag_redraw(); return {'FINISHED'}

class LLM4BLENDER_OT_TestSound(Operator):
    bl_idname = "llm4blender.test_sound"
    bl_label = "Test"
    def execute(self, context):
        play_notification_sound(context); return {'FINISHED'}

class LLM4BLENDER_OT_EditTemplate(bpy.types.Operator):
    bl_idname = "llm4blender.edit_template"
    bl_label = "Edit Template"
    bl_description = "Open current template file in the text editor"

    def execute(self, context):
        props = context.scene.llm4blender_props
        filepath = os.path.join(get_templates_dir(), props.template_type + ".txt")
        if not os.path.exists(filepath):
            self.report({'ERROR'}, f"Template file not found: {filepath}")
            return {'CANCELLED'}
        norm_path = os.path.normpath(filepath)
        for text in bpy.data.texts:
            if text.filepath and os.path.normpath(bpy.path.abspath(text.filepath)) == norm_path:
                context.space_data.text = text
                return {'FINISHED'}
        text = bpy.data.texts.load(filepath)
        context.space_data.text = text
        return {'FINISHED'}

class LLM4BLENDER_OT_AddTemplate(bpy.types.Operator):
    bl_idname = "llm4blender.add_template"
    bl_label = "Add Template"
    bl_description = "Create a new template file in the templates folder"

    name: bpy.props.StringProperty(name="Name", default="My Template")

    def execute(self, context):
        name = self.name.strip()
        if not name:
            self.report({'ERROR'}, "Template name cannot be empty.")
            return {'CANCELLED'}
        tdir = get_templates_dir()
        os.makedirs(tdir, exist_ok=True)
        filepath = os.path.join(tdir, name + ".txt")
        text = bpy.data.texts.new(name + ".txt")
        text.filepath = filepath
        context.space_data.text = text
        return {'FINISHED'}

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self)

    def draw(self, context):
        self.layout.prop(self, "name")

class LLM4BLENDER_OT_RefreshTemplates(bpy.types.Operator):
    bl_idname = "llm4blender.refresh_templates"
    bl_label = "Refresh Templates"
    bl_description = "Reload template list from the templates folder"

    def execute(self, context):
        refresh_templates_cache()
        context.region.tag_redraw()
        return {'FINISHED'}

class LLM4BLENDER_PT_Panel(bpy.types.Panel):
    bl_label = "LLM4 Blender"
    bl_idname = "LLM4BLENDER_PT_Panel"
    bl_space_type = 'TEXT_EDITOR'
    bl_region_type = 'UI'
    bl_category = "LLM4Blender"

    def draw(self, context):
        layout = self.layout
        props = context.scene.llm4blender_props
        wm = context.window_manager
        
        # --- SERVER STATUS HEADER ---
        if SERVER_ONLINE:
            pass
            #status_row.label(text="Ollama: Online", icon='CHECKMARK')
        else:
            status_box = layout.box()
            status_row = status_box.row()
            status_box.alert = True
            status_row.label(text="Ollama: OFFLINE", icon='ERROR')
            status_box.label(text="Please start the Ollama App", icon='INFO')
            col = status_box.column()
            col.scale_y = 0.8
            col.operator("llm4blender.refresh_models", text="Retry Connection", icon='FILE_REFRESH')
        
            layout.separator()

        # --- MAIN CONTROLS ---
        main_col = layout.column()
        row = main_col.row(align=True)
        row.prop(props, "mode", expand=True)

        main_col.separator()

        if props.mode == 'CHAT':
            
            if not SERVER_ONLINE:
                main_col.enabled = False

            box = main_col.box()
            row = box.row(align=True)
            row.prop(props, "model_selector", text="")
            row.operator("llm4blender.refresh_models", text="", icon='FILE_REFRESH')

            row = box.row(align=True)
            if props.is_session_active:
                row.operator("llm4blender.session_control", text="Active (Unload)", icon='PAUSE').action = 'STOP'
                row.alert = True
            else:
                row.operator("llm4blender.session_control", text="Start Session (Load)", icon='PLAY').action = 'START'

            if props.model_selector == "NONE":
                main_col.label(text="No models found", icon='INFO'); return            
            
            box = main_col.box()
            row = box.row(align=True)
            row.prop(props, "template_type", text="Role")
            row.operator("llm4blender.edit_template", text="", icon='GREASEPENCIL')
            row.operator("llm4blender.add_template", text="", icon='ADD')
            row.operator("llm4blender.refresh_templates", text="", icon='FILE_REFRESH')

            box = main_col.box()
            box = box.column(align=True)
            box.label(text="Chat / Continue")
            col = box.column(align=True)
            col.textbox(props, "prompt_input", placeholder='Positive prompt...')
            #col.prop(props, "prompt_input", text="")
            op = col.operator("llm4blender.generate", icon='PLAY', text="Generate")
            op.mode = "CHAT"
            
            box = main_col.box()
            box = box.column(align=True)
            if props.template_type == 'PYTHON': rewrite_label = "Refactor Code"
            elif props.template_type == 'ADDON': rewrite_label = "Refactor Addon"
            elif props.template_type == 'FOUNTAIN': rewrite_label = "Rewrite Scene"
            elif props.template_type == 'DIALOGUE': rewrite_label = "Rewrite Dialogue"
            elif props.template_type == 'CHARACTER': rewrite_label = "Revise Character"
            elif props.template_type == 'TREATMENT': rewrite_label = "Revise Treatment"
            elif props.template_type == 'IMAGE': rewrite_label = "Generate Shot List"
            elif props.template_type == 'IDEOGRAM4': rewrite_label = "Convert to Ideogram 4 JSON"
            elif props.template_type == 'CINEMASCOPE_PROMPT': rewrite_label = "Generate Cinemascope Prompts"
            elif props.template_type == 'LTX_VIDEO': rewrite_label = "Generate LTX 2.3 Prompts"
            elif props.template_type == 'SCREENPLAY_SHOTS': rewrite_label = "Insert Shots into Script"
            elif props.template_type == 'CINEMATIC_PROSE': rewrite_label = "Rewrite as Cinematic Prose"
            elif props.template_type == 'TTS_CLEANUP': rewrite_label = "Convert to TTS"
            elif props.template_type.startswith('YOUTUBE_'): rewrite_label = "Generate YouTube Content"
            else: rewrite_label = "Rewrite File"
            
            box.label(text=rewrite_label)
            col = box.column(align=True)
            if props.template_type not in ['IMAGE', 'IDEOGRAM4', 'SCREENPLAY_SHOTS']:
                #col.prop(props, "rewrite_prefix", text="")
                col.textbox(props, "rewrite_prefix", placeholder='Rework this...')
            
            op = col.operator("llm4blender.generate", icon='TEXT', text=rewrite_label)
            op.mode = "REWRITE"
  
              # --- PROGRESS BAR (Global) ---
            if props.status_message != "Ready" and props.status_message != "":
                box = layout.box()
                box = box.column(align=True)
                box.label(text=props.status_message, icon='TIME')
                if props.progress > 0:
                    box.prop(props, "progress", text="Progress", slider=True)
            
            if len(props.chat_history) > 0:
                hist_box = layout.box()
                hist_box = hist_box.column(align=True)
                hist_box.label(text=f"History")
                for i, item in enumerate(reversed(props.chat_history)):
                    idx = len(props.chat_history) - 1 - i
                    box = hist_box.box()
                    row = box.row()
                    row.label(text="Input:")
                    sub = row.row(align=True)
                    cp = sub.operator("llm4blender.copy_history", text="", icon="COPYDOWN")
                    cp.index = idx
                    rm = sub.operator("llm4blender.remove_history", text="", icon="TRASH")
                    rm.index = idx
                    box.label(text=item.input, icon='CONSOLE')

            box = layout.box()
            box = box.column(align=True)
            row = box.row(align=True)
            row.prop(props, "temperature", slider=True)
            row = box.row(align=True)
            row.prop(props, "use_random_seed", text="Random Seed")
            if not props.use_random_seed:
                row.prop(props, "seed", text="")
                
        elif props.mode == 'IMPORT':
            main_col.label(text="HuggingFace GGUF Import")
            main_col.prop(props, "custom_url", text="URL")
            main_col.prop(props, "custom_name", text="Name")
            main_col.operator("llm4blender.import_gguf", text="Download & Install", icon='URL')
            main_col.separator()
            main_col.label(text="Download Folder:")
            main_col.label(text=get_download_path(), icon='FILE_FOLDER')

            # --- PROGRESS BAR (Global) ---
            if props.status_message != "Ready" and props.status_message != "":
                box = layout.box()
                box = box.column(align=True)
                box.label(text=props.status_message, icon='TIME')
                if props.progress > 0:
                    box.prop(props, "progress", text="Progress", slider=True)
            
        box = layout.box()
        box = box.column(align=True)
        row = box.row(align=True)
        box.prop(props, "play_sound", text="Play Sound")
        if props.play_sound:
            row = box.row(); row.prop(props, "sound_select", text=""); row.operator("llm4blender.test_sound", text="", icon='PLAY')

class LLM4BlenderProperties(bpy.types.PropertyGroup):
    mode: EnumProperty(items=[('CHAT', "Chat", ""), ('IMPORT', "Settings", "")], default='CHAT')
    template_type: EnumProperty(name="Template", items=get_template_items)
    prompt_input: StringProperty(name="Prompt", default="")
    rewrite_prefix: StringProperty(name="Prefix", default="")
    model_selector: EnumProperty(name="Model", items=get_cached_models)
    is_session_active: BoolProperty(default=False)
    chat_history: CollectionProperty(type=ChatHistoryItem)
    custom_url: StringProperty(name="URL", default="")
    custom_name: StringProperty(name="Name", default="MyModel")
    play_sound: BoolProperty(default=True)
    sound_select: EnumProperty(items=[("ding", "Ding", ""), ("coin", "Coin", ""), ("user", "User", "")], default="ding")
    user_sound_path: StringProperty(subtype="FILE_PATH")
    temperature: FloatProperty(name="Temperature", default=0.7, min=0.0, max=1.0)
    use_random_seed: BoolProperty(name="Random Seed", default=True)
    seed: IntProperty(name="Seed", default=42)
    
    # UI Feedback Props
    status_message: StringProperty(default="Ready")
    progress: FloatProperty(default=0.0, min=0.0, max=1.0, subtype='PERCENTAGE')

classes = (
    LLM4BLENDER_OT_SessionControl, LLM4BLENDER_OT_TestSound, ChatHistoryItem,
    LLM4BlenderProperties, LLM4BLENDER_OT_Generate, LLM4BLENDER_OT_RemoveHistory,
    LLM4BLENDER_OT_CopyHistory, LLM4BLENDER_OT_ImportGGUF, LLM4BLENDER_OT_RefreshModels,
    LLM4BLENDER_OT_EditTemplate, LLM4BLENDER_OT_AddTemplate, LLM4BLENDER_OT_RefreshTemplates,
    LLM4BLENDER_PT_Panel,
)

def register():
    refresh_templates_cache()
    for cls in classes: bpy.utils.register_class(cls)
    bpy.types.Scene.llm4blender_props = PointerProperty(type=LLM4BlenderProperties)
    if not MODELS_CACHE or MODELS_CACHE[0][0] == "NONE":
        bpy.app.timers.register(lambda: (update_model_cache(), None)[1], first_interval=0.5)
    bpy.app.timers.register(_watch_templates_dir, first_interval=3.0, persistent=True)

def unregister():
    if bpy.app.timers.is_registered(_watch_templates_dir):
        bpy.app.timers.unregister(_watch_templates_dir)
    for cls in reversed(classes): bpy.utils.unregister_class(cls)
    del bpy.types.Scene.llm4blender_props

if __name__ == "__main__":
    register()
