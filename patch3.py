import re

def patch():
    with open('src/thread_save/storage/writer.py', 'r', encoding='utf-8') as f:
        text = f.read()

    text = text.replace(
        "current_file = Path(ps.file_path)",
        "current_file = Path(ps.file_path)\n        entry = self._registry.get(thread_id)"
    )
    
    text = text.replace(
        "        # Find the slot in the page\n        content = page_file.read_text(encoding=\"utf-8\")",
        "        # Find the slot in the page\n        entry = self._registry.get(thread_id)\n        content = page_file.read_text(encoding=\"utf-8\")"
    )

    with open('src/thread_save/storage/writer.py', 'w', encoding='utf-8') as f:
        f.write(text)

if __name__ == "__main__":
    patch()
