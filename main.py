import asyncio
import json
import httpx
import queue
import threading
from threading import Thread
import sounddevice as sd
import re
from kokoro import KPipeline
from multiprocessing import Queue
from typing import Optional, Dict
import time
from tracking import TrackingSystem
import traceback

def load_config():
    with open("config.json", "r", encoding="utf-8") as f:
        return json.load(f)

class AudioPlayer:
    def __init__(self, max_queue_size: int = 10):
        self.audio_queue = queue.Queue(maxsize=max_queue_size)
        self.is_speaking = False
        self.should_stop = False
        self._start_audio_thread()

    def _start_audio_thread(self):
        self.audio_thread = threading.Thread(target=self._audio_player_loop, daemon=True)
        self.audio_thread.start()

    def _audio_player_loop(self):
        while not self.should_stop:
            try:
                audio_data = self.audio_queue.get(timeout=1)
                if audio_data is None:
                    continue
                
                self.is_speaking = True
                sd.play(audio_data, samplerate=24000)
                sd.wait()
                self.is_speaking = False
                
            except queue.Empty:
                continue
            except Exception as e:
                print(f"Error playing audio: {e}")
                self.is_speaking = False

    def play(self, audio_data):
        try:
            self.audio_queue.put(audio_data, block=False)
        except queue.Full:
            print("Audio queue is full, skipping...")

    def stop(self):
        self.should_stop = True
        self.audio_queue.put(None)
        if self.audio_thread.is_alive():
            self.audio_thread.join(timeout=1)

class ResponseCache:
    def __init__(self, cache_size: int = 100, similarity_threshold: float = 0.8):
        self.cache: Dict[str, str] = {}
        self.cache_size = cache_size
        self.similarity_threshold = similarity_threshold

    def _compute_similarity(self, s1: str, s2: str) -> float:
        words1 = set(s1.lower().split())
        words2 = set(s2.lower().split())
        intersection = len(words1.intersection(words2))
        union = len(words1.union(words2))
        return intersection / union if union > 0 else 0

    def get(self, query: str) -> Optional[str]:
        for cached_query, response in self.cache.items():
            if self._compute_similarity(query, cached_query) > self.similarity_threshold:
                return response
        return None

    def add(self, query: str, response: str):
        if len(self.cache) >= self.cache_size:
            self.cache.pop(next(iter(self.cache)))
        self.cache[query] = response

class AIVtuber:
    def __init__(self, config, message_queue: Queue, tracking_system: TrackingSystem):
        self.config = config
        self.deepseek_api_key = config["api_settings"]["deepseek_api_key"]
        self.tts_pipeline = KPipeline(lang_code=config["voice_settings"]["language_code"])
        self.audio_player = AudioPlayer()
        self.message_queue = message_queue
        self.response_cache = ResponseCache()
        self.processing_lock = asyncio.Lock()
        self.last_response_time = 0
        self.rate_limit_delay = 1
        self.tracking = tracking_system
        
        with open("output.txt", "w", encoding="utf-8") as f:
            f.write("")

    def _write_subtitle(self, text: str):
        """Write response to output.txt, overwriting previous content"""
        try:
            with open("output.txt", "w", encoding="utf-8") as f:
                f.write(text)
        except Exception as e:
            self.tracking.track_error(f"Failed to write subtitle: {str(e)}")

    def _clean_response(self, text: str) -> str:
        """Clean and format the response text."""
        text = re.sub(r'[\U00010000-\U0010FFFF]', '', text, flags=re.UNICODE)
        text = re.sub(r'\*[^*]*\*', '', text)
        text = re.sub(r'^[^:]+:\s+', '', text)
        text = text.strip()

        if len(text) > self.config["api_settings"]["max_response_length"]:
            sentences = re.split(r'(?<=[.!?])\s+', text)
            result = ""
            for sentence in sentences:
                if len(result + sentence) <= self.config["api_settings"]["max_response_length"]:
                    result += sentence + " "
                else:
                    break
            return result.strip()
        
        return text

    async def _call_deepseek_api(self, user_message: str) -> str:
        """Call Deepseek API with optimized timeout and caching."""
        try:
            self.tracking.track_workflow("API", f"Starting API call for: {user_message[:50]}...")
            
            headers = {
                "Authorization": f"Bearer {self.deepseek_api_key}",
                "Content-Type": "application/json"
            }

            personality_guidelines = "Your personality traits:\n"
            for trait, description in self.config["character"]["personality_traits"].items():
                personality_guidelines += f"- {trait}: {description}\n"

            response_guidelines = "\nResponse handling guidelines:\n"
            for situation, details in self.config["character"]["response_handling"].items():
                response_guidelines += f"- For {situation}: {details['description']}\n"
                response_guidelines += f"  Example: {details['example']}\n"

            enhanced_prompt = (
                f"{self.config['character']['system_prompt']}\n\n"
                f"{personality_guidelines}\n"
                f"{response_guidelines}\n\n"
                "Remember to be concise and natural in your responses. "
                "Aim to complete your thoughts within 1-2 sentences while maintaining "
                "your characteristic wit and intelligence. "
                f"User message: {user_message}"
            )

            payload = {
                "model": self.config["api_settings"]["model"],
                "messages": [
                    {"role": "system", "content": enhanced_prompt},
                    {"role": "user", "content": user_message}
                ],
                "temperature": self.config["api_settings"]["temperature"],
                "max_tokens": 100
            }

            max_retries = 3
            for attempt in range(max_retries):
                try:
                    timeout = 15.0 * (attempt + 1)
                    self.tracking.track_workflow("API", f"Attempt {attempt + 1}/{max_retries} with {timeout}s timeout")
                    
                    async with httpx.AsyncClient(timeout=timeout) as client:
                        response = await client.post(
                            "https://api.deepseek.com/v1/chat/completions",
                            headers=headers,
                            json=payload
                        )
                        
                        if response.status_code == 200:
                            response_data = response.json()
                            self.tracking.track_workflow("API", "Success: Got response from API")
                            return self._clean_response(response_data["choices"][0]["message"]["content"])
                        else:
                            error_msg = f"API error {response.status_code}: {response.text}"
                            self.tracking.track_error(error_msg)
                            if attempt == max_retries - 1:
                                raise Exception(error_msg)
                            continue

                except (httpx.ReadTimeout, httpx.ConnectTimeout) as e:
                    self.tracking.track_error(f"Timeout on attempt {attempt + 1}: {str(e)}")
                    if attempt == max_retries - 1:
                        raise
                    await asyncio.sleep(1 * (attempt + 1))
                    continue

        except Exception as e:
            error_msg = f"API call failed after {max_retries} attempts: {str(e)}"
            self.tracking.track_error(error_msg)
            return "I apologize, but I'm having trouble connecting to my thoughts right now. Perhaps we could try again in a moment?"

    async def _text_to_speech_async(self, text: str) -> None:
        """Asynchronous TTS processing."""
        try:
            self.tracking.track_workflow("TTS", f"Converting text to speech: {text[:50]}...")
            def _generate_audio():
                generator = self.tts_pipeline(
                    text,
                    voice=self.config["voice_settings"]["voice_id"],
                    speed=self.config["voice_settings"]["speed"]
                )
                return next(generator)[2]

            audio = await asyncio.get_event_loop().run_in_executor(None, _generate_audio)
            if audio is not None:
                self.audio_player.play(audio.numpy())
        except Exception as e:
            self.tracking.track_error(f"TTS error: {str(e)}")

    async def process_chat(self):
        """Process chat messages with rate limiting and concurrent processing."""
        print("Starting chat processing...")
        pending_tasks = set()

        async def process_message(chat_item):
            try:
                async with self.processing_lock:
                    current_time = time.time()
                    if current_time - self.last_response_time < self.rate_limit_delay:
                        await asyncio.sleep(self.rate_limit_delay - (current_time - self.last_response_time))

                    username = chat_item["author"]
                    message = chat_item["message"]
                    print(f"Processing message from {username}: {message}")

                    print("Calling Deepseek API...")
                    response = await self._call_deepseek_api(message)
                    print(f"API Response received: {response}")

                    if response:
                        self._write_subtitle(response)
                        
                        print("Converting to speech...")
                        await self._text_to_speech_async(response)
                        print("Speech conversion complete")
                    else:
                        print("Received empty response from API")

                    self.last_response_time = time.time()
            except Exception as e:
                print(f"Error in process_message: {e}")
                traceback.print_exc()

        while True:
            try:
                if not self.message_queue.empty():
                    chat_item = self.message_queue.get()
                    print(f"Got message from queue: {chat_item}")
                    
                    task = asyncio.create_task(process_message(chat_item))
                    pending_tasks.add(task)
                    task.add_done_callback(pending_tasks.discard)

                done_tasks = {task for task in pending_tasks if task.done()}
                for task in done_tasks:
                    if task.exception():
                        print(f"Task failed with error: {task.exception()}")
                pending_tasks.difference_update(done_tasks)

                await asyncio.sleep(0.1)
            except Exception as e:
                print(f"Error in main loop: {e}")
                traceback.print_exc()

    async def run(self):
        print("Starting AI Vtuber...")
        try:
            await self.process_chat()
        except asyncio.CancelledError:
            print("AI process was interrupted.")
        finally:
            self.audio_player.stop()

def main():
    config = load_config()
    print("Config loaded:", config)
    message_queue = Queue()

    tracking_system = TrackingSystem(
        video_id=config["youtube_settings"]["video_id"],
        message_queue=message_queue
    )

    try:

        vtuber = AIVtuber(config, message_queue, tracking_system)
        print("AI Vtuber initialized")
        
        def run_vtuber():
            asyncio.run(vtuber.run())

        vtuber_thread = Thread(target=run_vtuber, daemon=True)
        vtuber_thread.start()

        tracking_system.root.mainloop()

    except KeyboardInterrupt:
        print("\nShutting down AI Vtuber...")
    except Exception as e:
        print(f"Error in main: {e}")
        traceback.print_exc()
    finally:
        tracking_system.save_tracking_data()
        print("AI Vtuber stopped successfully.")

if __name__ == "__main__":
    main()
