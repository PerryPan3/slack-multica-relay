import json
import sys
import tempfile
import unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from agent_profile import load_profile, profile_prompt, ProfileError


class ProfileTests(unittest.TestCase):
    def test_current_profile_is_reloaded_instead_of_reusing_thread_preferences(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);(root/'agent').mkdir()
            (root/'agent/config.json').write_text(json.dumps({'profile_file':'AGENTS.md'}))
            path=root/'agent/AGENTS.md';path.write_text('Use complete sentences.')
            first=load_profile(root)
            path.write_text('Lead with the decision; explain it naturally.')
            second=load_profile(root)
            self.assertNotEqual(first['sha256'],second['sha256'])
            self.assertIn(second['text'],profile_prompt(second))
            self.assertNotIn(first['text'],profile_prompt(second))

    def test_configured_profile_never_silently_disappears_or_escapes_private_directory(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);(root/'agent').mkdir()
            config=root/'agent/config.json'
            self.assertEqual(load_profile(root)['text'],'')
            for value in ['missing.md','../outside.md','']:
                config.write_text(json.dumps({'profile_file':value}))
                with self.assertRaises(ProfileError):load_profile(root)
            config.write_text(json.dumps({'profile_file':'AGENTS.md'}))
            path=root/'agent/AGENTS.md'
            for content in ['', 'x'*65537]:
                path.write_text(content)
                with self.assertRaises(ProfileError):load_profile(root)
