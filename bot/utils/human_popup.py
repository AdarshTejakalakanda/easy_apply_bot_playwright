"""
On-Screen Human-in-the-Loop Popup Window
Appears centered on top of all windows with a 10-minute auto-countdown timer.
"""

import tkinter as tk
from tkinter import ttk
import time

try:
    import winsound
except ImportError:
    winsound = None


class HumanPromptPopup:
    def __init__(self, question_text: str, field_type: str = "text", options: list = None, 
                 suggested_answer: str = None, is_required: bool = False, timeout_seconds: int = 600,
                 data_type: str = None, html_spec: dict = None):
        self.question_text = question_text
        self.field_type = field_type
        self.options = options or []
        self.suggested_answer = suggested_answer or ""
        self.is_required = is_required
        self.timeout_seconds = timeout_seconds  # 10 minutes = 600s
        self.time_left = timeout_seconds
        self.data_type = data_type or field_type.upper()
        self.html_spec = html_spec or {}
        
        self.result = None
        self.is_closed = False
        self.root = None
        
    def show(self) -> str or None:
        """Create and display the popup window on top of all other windows"""
        try:
            root = tk.Tk()
            self.root = root
            root.title("🤖 LinkedIn Bot - Action Required")
            
            # Dimensions and centering
            width = 600
            height = 500 if (self.options and len(self.options) > 2) else 440
            screen_width = root.winfo_screenwidth()
            screen_height = root.winfo_screenheight()
            x = int((screen_width - width) / 2)
            y = int((screen_height - height) / 2)
            root.geometry(f"{width}x{height}+{x}+{y}")
            root.minsize(540, 390)
            
            # Make window stay on top and grab focus
            root.attributes('-topmost', True)
            root.lift()
            root.focus_force()
            root.configure(bg="#181825")
            
            # Play gentle notification chime
            try:
                if winsound:
                    winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
                else:
                    root.bell()
            except Exception:
                pass
            
            # Header Frame
            header_frame = tk.Frame(root, bg="#11111b", pady=12, padx=16)
            header_frame.pack(fill="x")
            
            req_text = " ⚠️ (REQUIRED)" if self.is_required else ""
            title_lbl = tk.Label(header_frame, text=f"🤔 Action Required: Unknown Question{req_text}", 
                                 font=("Segoe UI", 12, "bold"), fg="#f38ba8" if self.is_required else "#fab387", bg="#11111b")
            title_lbl.pack(anchor="w")
            
            timer_lbl = tk.Label(header_frame, text="⏳ Auto-submitting in 10:00", 
                                 font=("Segoe UI", 10), fg="#a6adc8", bg="#11111b")
            timer_lbl.pack(anchor="w", pady=(2, 0))
            self.timer_lbl = timer_lbl

            # Main Content Frame
            content_frame = tk.Frame(root, bg="#181825", padx=20, pady=10)
            content_frame.pack(fill="both", expand=True)

            # Question Text Box
            q_title = tk.Label(content_frame, text="QUESTION:", font=("Segoe UI", 9, "bold"), fg="#89b4fa", bg="#181825")
            q_title.pack(anchor="w")

            q_frame = tk.Frame(content_frame, bg="#313244", padx=10, pady=10, relief="flat")
            q_frame.pack(fill="x", pady=(4, 8))
            
            q_lbl = tk.Label(q_frame, text=self.question_text, font=("Segoe UI", 10), fg="#cdd6f4", bg="#313244", 
                             wraplength=530, justify="left")
            q_lbl.pack(anchor="w")

            # HTML Element & DOM Expected Type Card (Directly from the live web page DOM)
            spec_tag = self.html_spec.get('tag', f"<{self.field_type}>")
            spec_desc = self.html_spec.get('description', self.data_type)
            spec_details = self.html_spec.get('details', '')
            spec_placeholder = self.html_spec.get('placeholder', '')
            
            type_frame = tk.Frame(content_frame, bg="#1e1e2e", padx=12, pady=8, highlightbackground="#fab387", highlightthickness=1)
            type_frame.pack(fill="x", pady=(0, 10))

            header_lbl = tk.Label(type_frame, text="🌐 PAGE HTML FIELD REQUIREMENT (FROM LIVE DOM):", 
                                  font=("Segoe UI", 9, "bold"), fg="#fab387", bg="#1e1e2e")
            header_lbl.pack(anchor="w")

            spec_line1 = f"• HTML Element: {spec_tag}   |   Expected Type: {self.data_type} ({spec_desc})"
            lbl1 = tk.Label(type_frame, text=spec_line1, font=("Segoe UI", 9, "bold"), fg="#a6e3a1", bg="#1e1e2e", wraplength=530, justify="left")
            lbl1.pack(anchor="w", pady=(2, 0))

            if spec_details:
                lbl2 = tk.Label(type_frame, text=f"• Specification: {spec_details}", font=("Segoe UI", 9), fg="#cdd6f4", bg="#1e1e2e", wraplength=530, justify="left")
                lbl2.pack(anchor="w", pady=(2, 0))

            if spec_placeholder:
                lbl3 = tk.Label(type_frame, text=f"• Placeholder: \"{spec_placeholder}\"", font=("Segoe UI", 9, "italic"), fg="#89dceb", bg="#1e1e2e", wraplength=530, justify="left")
                lbl3.pack(anchor="w", pady=(2, 0))

            # Input Section
            self.selected_val = tk.StringVar(value=self.suggested_answer)

            if self.options and self.field_type in ["select", "radio"]:
                ans_title = tk.Label(content_frame, text="SELECT AN OPTION:", font=("Segoe UI", 9, "bold"), fg="#a6e3a1", bg="#181825")
                ans_title.pack(anchor="w")
                
                # If few options (<= 4), show radio buttons, else dropdown
                if len(self.options) <= 4:
                    opts_frame = tk.Frame(content_frame, bg="#181825")
                    opts_frame.pack(fill="x", pady=(4, 8))
                    for opt in self.options:
                        rb = tk.Radiobutton(opts_frame, text=opt, variable=self.selected_val, value=opt,
                                            font=("Segoe UI", 10), fg="#cdd6f4", bg="#181825", 
                                            selectcolor="#313244", activebackground="#181825", activeforeground="#a6e3a1")
                        rb.pack(anchor="w", pady=2)
                else:
                    combo = ttk.Combobox(content_frame, values=self.options, textvariable=self.selected_val, 
                                         font=("Segoe UI", 11), state="readonly")
                    combo.pack(fill="x", pady=(4, 8))
            else:
                ans_title = tk.Label(content_frame, text="YOUR ANSWER:", font=("Segoe UI", 9, "bold"), fg="#a6e3a1", bg="#181825")
                ans_title.pack(anchor="w")

                entry = tk.Entry(content_frame, textvariable=self.selected_val, font=("Segoe UI", 11), 
                                 bg="#313244", fg="#cdd6f4", insertbackground="#cdd6f4", relief="flat", bd=6)
                entry.pack(fill="x", pady=(4, 8))
                entry.focus_set()
                entry.select_range(0, tk.END)

            # AI Suggestion note if available
            if self.suggested_answer:
                ai_note = tk.Label(content_frame, text=f"💡 AI Suggestion: \"{self.suggested_answer}\" (Pre-filled)", 
                                   font=("Segoe UI", 9, "italic"), fg="#89dceb", bg="#181825")
                ai_note.pack(anchor="w", pady=(0, 8))

            # Buttons Frame
            btn_frame = tk.Frame(root, bg="#11111b", pady=14, padx=20)
            btn_frame.pack(fill="x", side="bottom")

            skip_btn = tk.Button(btn_frame, text="⏭️ Skip", command=self._on_skip,
                                 font=("Segoe UI", 10), bg="#45475a", fg="#cdd6f4", activebackground="#585b70",
                                 activeforeground="#ffffff", relief="flat", padx=14, pady=6, cursor="hand2")
            skip_btn.pack(side="left")

            submit_btn = tk.Button(btn_frame, text="✅ Submit Answer", command=self._on_submit,
                                   font=("Segoe UI", 10, "bold"), bg="#a6e3a1", fg="#11111b", activebackground="#94e2d5",
                                   activeforeground="#11111b", relief="flat", padx=18, pady=6, cursor="hand2")
            submit_btn.pack(side="right")

            if self.suggested_answer:
                use_ai_btn = tk.Button(btn_frame, text="🤖 Use AI Answer", command=self._on_accept_ai,
                                       font=("Segoe UI", 10), bg="#89b4fa", fg="#11111b", activebackground="#74c7ec",
                                       activeforeground="#11111b", relief="flat", padx=14, pady=6, cursor="hand2")
                use_ai_btn.pack(side="right", padx=(0, 10))

            # Keyboard bindings
            root.bind("<Return>", lambda e: self._on_submit())
            root.bind("<Escape>", lambda e: self._on_skip())

            # Start timer countdown loop
            self._update_timer()

            # Main loop
            root.mainloop()

        except Exception as e:
            # Fallback if GUI fails
            return self.suggested_answer if self.suggested_answer else None

        return self.result

    def _update_timer(self):
        if self.is_closed or not self.root:
            return
        
        mins = self.time_left // 60
        secs = self.time_left % 60
        timer_text = f"{mins:02d}:{secs:02d}"
        
        if self.suggested_answer:
            self.timer_lbl.config(text=f"⏳ Auto-submitting AI suggestion in: {timer_text}")
        else:
            self.timer_lbl.config(text=f"⏳ Auto-skipping in: {timer_text}")
            
        if self.time_left <= 0:
            # Timeout reached -> Auto-proceed with suggested answer or None
            self.result = self.suggested_answer if self.suggested_answer else None
            self._close_window()
            return
            
        self.time_left -= 1
        self.root.after(1000, self._update_timer)

    def _on_submit(self):
        val = self.selected_val.get().strip()
        self.result = val if val else (self.suggested_answer if self.suggested_answer else None)
        self._close_window()

    def _on_accept_ai(self):
        self.result = self.suggested_answer
        self._close_window()

    def _on_skip(self):
        self.result = None
        self._close_window()

    def _close_window(self):
        self.is_closed = True
        try:
            if self.root:
                self.root.destroy()
        except Exception:
            pass


def prompt_human_with_popup(question_text: str, field_type: str = "text", options: list = None, 
                            suggested_answer: str = None, is_required: bool = False, timeout_seconds: int = 600,
                            data_type: str = None, html_spec: dict = None) -> str or None:
    """Helper function to trigger on-screen popup"""
    popup = HumanPromptPopup(
        question_text=question_text,
        field_type=field_type,
        options=options,
        suggested_answer=suggested_answer,
        is_required=is_required,
        timeout_seconds=timeout_seconds,
        data_type=data_type,
        html_spec=html_spec
    )
    return popup.show()
