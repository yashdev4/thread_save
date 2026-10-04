import os
import glob
import re

def patch_tests():
    files = glob.glob('tests/test_*.py') + ['run_tests.py', 'src/thread_save/server.py']
    
    for f in files:
        if not os.path.exists(f): continue
        with open(f, 'r', encoding='utf-8') as file:
            content = file.read()
            
        # replace import
        content = re.sub(
            r'from thread_save\.storage\.writer import FileStore',
            r'from thread_save.storage.writer import FileStore\nfrom thread_save.engine import ThreadVaultEngine',
            content
        )
        
        # replace engine = FileStore(...)
        content = re.sub(
            r'engine = FileStore\((.*?)\)',
            r'engine = ThreadVaultEngine(FileStore(\1))',
            content
        )
        
        with open(f, 'w', encoding='utf-8') as file:
            file.write(content)
            
if __name__ == "__main__":
    patch_tests()
