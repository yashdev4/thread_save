import re

def patch():
    with open('src/thread_save/fsck.py', 'r', encoding='utf-8') as f:
        text = f.read()

    text = re.sub(r'def verify_vault\(vault_path: str = \'vault\'\) -> int:', 'def verify_vault(vault_path: str = \'vault\') -> int:\n    files_scanned = 0\n    threads_scanned = 0\n    turns_scanned = 0', text)

    text = text.replace('for tid, pages in thread_pages.items():', 'for tid, pages in thread_pages.items():\n        threads_scanned += 1')
    text = text.replace('if page_file.exists():', 'if page_file.exists():\n                    files_scanned += 1')
    text = text.replace('if match:', 'if match:\n                                turns_scanned += 1')

    text = text.replace('if not violations:', 'print(f"Scanned {files_scanned} files across {threads_scanned} threads ({turns_scanned} turns).")\n    if files_scanned == 0:\n        print("Error: 0 files scanned.")\n        return 1\n    if not violations:')

    with open('src/thread_save/fsck.py', 'w', encoding='utf-8') as f:
        f.write(text)

if __name__ == '__main__':
    patch()
