import tkinter as tk
from tkinter import ttk
import json
import os
from datetime import datetime
import queue
import pytchat

class TrackingSystem:
    def __init__(self, video_id: str, message_queue: queue.Queue):
        self.tracking_dir = "tracking"
        os.makedirs(self.tracking_dir, exist_ok=True)
        
        self.workflow_queue = queue.Queue()
        self.error_queue = queue.Queue()
        self.message_queue = message_queue
        self.video_id = video_id
        self.chat = None
        
        self.tracking_data = {
            "workflow": [],
            "errors": [],
            "chat_messages": []
        }
        
        self.root = tk.Tk()
        self.root.title("Corelia Tracking System")
        self.root.geometry("1200x600")
        self.setup_ui()
        
        try:
            self.chat = pytchat.create(video_id=self.video_id)
            self.root.after(100, self._check_chat)
        except Exception as e:
            self.track_error(f"Failed to initialize chat: {str(e)}")
        
        self.root.after(100, self.update)

    def setup_ui(self):
        main_container = ttk.PanedWindow(self.root, orient=tk.HORIZONTAL)
        main_container.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        left_container = ttk.Frame(main_container)
        main_container.add(left_container, weight=2)

        workflow_frame = ttk.LabelFrame(left_container, text="Workflow Timeline")
        workflow_frame.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        self.workflow_text = tk.Text(workflow_frame, wrap=tk.WORD, width=40)
        self.workflow_text.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        error_frame = ttk.LabelFrame(left_container, text="Error Log")
        error_frame.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        self.error_text = tk.Text(error_frame, wrap=tk.WORD, width=40, fg="red")
        self.error_text.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        chat_frame = ttk.LabelFrame(main_container, text="YouTube Chat")
        main_container.add(chat_frame, weight=1)

        self.chat_text = tk.Text(chat_frame, wrap=tk.WORD, width=30)
        self.chat_text.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        for text_widget in (self.workflow_text, self.error_text, self.chat_text):
            text_widget.configure(state=tk.DISABLED)

        self.root.protocol("WM_DELETE_WINDOW", self.on_closing)

    def _check_chat(self):
        """Check for new chat messages in the main thread."""
        try:
            if self.chat and self.chat.is_alive():
                for chat_item in self.chat.get().sync_items():
                    message_data = {
                        "author": chat_item.author.name,
                        "message": chat_item.message
                    }
                    self.tracking_data["chat_messages"].append({
                        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        **message_data
                    })
                    self._add_chat_entry(message_data["author"], message_data["message"])
                    self.message_queue.put(message_data)
        except Exception as e:
            self.track_error(f"Chat reader error: {str(e)}")
        finally:
            self.root.after(100, self._check_chat)

    def _add_chat_entry(self, author: str, message: str):
        """Add a chat message to the UI."""
        timestamp = datetime.now().strftime("%H:%M:%S")
        entry = f"[{timestamp}] {author}: {message}\n"
        
        self.chat_text.configure(state=tk.NORMAL)
        self.chat_text.insert(tk.END, entry)
        self.chat_text.see(tk.END)
        self.chat_text.configure(state=tk.DISABLED)

    def update(self):
        """Process queued updates"""
        try:
            while not self.workflow_queue.empty():
                phase, details = self.workflow_queue.get_nowait()
                self._add_workflow_entry(phase, details)

            while not self.error_queue.empty():
                error_msg = self.error_queue.get_nowait()
                self._add_error_entry(error_msg)

            if datetime.now().second == 0:
                self.save_tracking_data()

        except Exception as e:
            print(f"Error in update: {e}")

        self.root.after(100, self.update)

    def _add_workflow_entry(self, phase: str, details: str):
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        entry = f"[{timestamp}] {phase}: {details}\n"
        
        self.tracking_data["workflow"].append({
            "timestamp": timestamp,
            "phase": phase,
            "details": details
        })

        self.workflow_text.configure(state=tk.NORMAL)
        self.workflow_text.insert(tk.END, entry)
        self.workflow_text.see(tk.END)
        self.workflow_text.configure(state=tk.DISABLED)

    def _add_error_entry(self, error_msg: str):
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        entry = f"[{timestamp}] ERROR: {error_msg}\n"
        
        self.tracking_data["errors"].append({
            "timestamp": timestamp,
            "error": error_msg
        })

        self.error_text.configure(state=tk.NORMAL)
        self.error_text.insert(tk.END, entry)
        self.error_text.see(tk.END)
        self.error_text.configure(state=tk.DISABLED)

    def track_workflow(self, phase: str, details: str):
        """Add a workflow tracking entry"""
        self.workflow_queue.put((phase, details))

    def track_error(self, error_msg: str):
        """Add an error tracking entry"""
        self.error_queue.put(error_msg)

    def save_tracking_data(self):
        """Save tracking data to JSON files"""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        
        tracking_file = os.path.join(self.tracking_dir, f"tracking_{timestamp}.json")
        with open(tracking_file, 'w', encoding='utf-8') as f:
            json.dump(self.tracking_data, f, indent=2)

    def on_closing(self):
        """Handle window closing"""
        self.save_tracking_data()
        if self.chat:
            self.chat = None
        self.root.destroy()

    def start(self):
        """Start the tracking system"""
        self.root.mainloop() 