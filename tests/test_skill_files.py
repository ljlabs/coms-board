"""Skill file management uses temporary folders and an isolated board."""
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from coms import api, db, mcp, skill_store
from coms.server import r_skills


class TestSkillFiles(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.root_patch = patch.object(skill_store, 'SKILLS_ROOT', root / 'skills')
        self.root_patch.start()
        self.addCleanup(self.root_patch.stop)
        self.env = patch.dict(os.environ, {'COMS_DB': str(root / 'board.db'), 'COMS_AGENT': 'admin'})
        self.env.start()
        self.addCleanup(self.env.stop)
        db._local.__dict__.clear()
        self.conn = db.connect()
        self.addCleanup(self.close_db)
        api.save_skill(self.conn, 'example', 'Example', 'Use supporting references.', 'admin')

    def close_db(self):
        cached = getattr(db._local, 'conn', None)
        if cached is not None and cached is not self.conn:
            cached.close()
        self.conn.close()
        db._local.__dict__.clear()

    def version(self):
        return api.get_skill(self.conn, 'example')['tree_version']

    def create(self, path, kind='file', content=''):
        return api.create_skill_entry(self.conn, 'example', path, kind, 'admin', self.version(), content)

    def move(self, source, destination, version=None):
        return api.move_skill_entry(self.conn, 'example', source, destination, 'admin',
                                    self.version() if version is None else version)

    def test_empty_folders_and_nested_text_files_are_listed(self):
        self.create('references', 'folder')
        result = self.create('references/evidence', 'folder')
        self.assertEqual(result['directories'], ['references', 'references/evidence'])
        self.assertEqual(result['file_count'], 1)
        self.create('references/evidence/notes.md', content='Primary evidence.')
        self.assertEqual(api.get_skill_file(self.conn, 'example', 'references/evidence/notes.md')['content'],
                         'Primary evidence.')

    def test_rename_and_move_preserve_content_and_allow_root_destination(self):
        self.create('a', 'folder')
        self.create('b', 'folder')
        self.create('a/notes.md', content='Do not lose this content.')
        self.move('a/notes.md', 'a/research.md')
        self.move('a/research.md', 'b/research.md')
        result = self.move('b/research.md', 'research.md')
        self.assertEqual(result['path'], 'research.md')
        self.assertFalse((skill_store.SKILLS_ROOT / 'example/b/research.md').exists())
        self.assertEqual(api.get_skill_file(self.conn, 'example', 'research.md')['content'],
                         'Do not lose this content.')

    def test_folders_move_with_nested_files_and_empty_directories(self):
        self.create('a', 'folder')
        self.create('a/empty', 'folder')
        self.create('a/notes.txt', content='Nested file')
        self.create('b', 'folder')
        result = self.move('a', 'b/renamed')
        self.assertIn('b/renamed/empty', result['directories'])
        self.assertEqual(api.get_skill_file(self.conn, 'example', 'b/renamed/notes.txt')['content'], 'Nested file')

    def test_moves_preserve_binary_contents(self):
        self.create('assets', 'folder')
        raw = b'\x00\xff\x89PNG binary data'
        origin = skill_store.SKILLS_ROOT / 'example/assets/image.png'
        origin.write_bytes(raw)
        self.move('assets/image.png', 'image.png')
        self.assertEqual((skill_store.SKILLS_ROOT / 'example/image.png').read_bytes(), raw)

    def test_creation_respects_file_and_folder_limits(self):
        with patch.object(skill_store, 'MAX_SKILL_FILES', 1), self.assertRaises(api.BadRequest):
            self.create('extra.md')
        self.create('docs', 'folder')
        with patch.object(skill_store, 'MAX_SKILL_DIRECTORIES', 1), self.assertRaises(api.BadRequest):
            self.create('other', 'folder')
        with patch.object(skill_store, 'MAX_SKILL_BYTES', 1), self.assertRaises(api.BadRequest):
            self.create('extra.md', content='Too large')

    def test_manifest_cannot_be_moved_or_replaced(self):
        self.create('folder', 'folder')
        self.create('other.md', content='Different file')
        for source, destination in [('SKILL.md', 'folder/SKILL.md'), ('SKILL.md', 'other.md'),
                                    ('other.md', 'SKILL.md'), ('other.md', 'skill.md')]:
            with self.subTest(source=source, destination=destination), self.assertRaises(api.BadRequest):
                self.move(source, destination)
        self.assertTrue(api.get_skill(self.conn, 'example')['body'])

    def test_name_collisions_never_overwrite_entries(self):
        self.create('notes.md', content='Original')
        self.create('other.md', content='Other')
        for source, destination in [('other.md', 'notes.md'), ('other.md', 'NOTES.md')]:
            with self.assertRaises(api.BadRequest):
                self.move(source, destination)
        with self.assertRaises(api.BadRequest):
            self.create('notes.md', content='Overwrite')
        self.assertEqual(api.get_skill_file(self.conn, 'example', 'notes.md')['content'], 'Original')

    def test_unsafe_paths_and_descendant_moves_are_rejected(self):
        self.create('docs', 'folder')
        self.create('docs/nested', 'folder')
        for path in ['../outside', '/absolute', 'docs/../outside', 'docs//file',
                     'docs/./file', 'docs\\file', '.disabled', 'docs/', 'CON.txt', 'name.']:
            with self.subTest(path=path), self.assertRaises(api.BadRequest):
                self.create(path)
        with self.assertRaises(api.BadRequest):
            self.move('docs', 'docs/nested/docs')
        with self.assertRaises(api.NotFound):
            self.move('docs', 'missing/docs')

    def test_stale_tree_revision_rejects_changes_without_losing_files(self):
        self.create('notes.md', content='Original')
        revision = self.version()
        api.write_skill_file(self.conn, 'example', 'notes.md', 'New content', 'admin')
        with self.assertRaises(api.BadRequest):
            self.move('notes.md', 'renamed.md', revision)
        with self.assertRaises(api.BadRequest):
            api.create_skill_entry(self.conn, 'example', 'extra', 'folder', 'admin', revision)
        self.assertEqual(api.get_skill_file(self.conn, 'example', 'notes.md')['content'], 'New content')

    def test_symlink_entries_and_folders_containing_symlinks_are_rejected(self):
        self.create('docs', 'folder')
        outside = Path(self.tmp.name) / 'outside.txt'
        outside.write_text('Outside', encoding='utf-8')
        link = skill_store.SKILLS_ROOT / 'example/docs/link.txt'
        try:
            link.symlink_to(outside)
        except OSError:
            self.skipTest('This platform does not allow creating symlinks')
        with self.assertRaises(api.BadRequest):
            self.move('docs', 'renamed')
        with self.assertRaises(api.BadRequest):
            self.move('docs/link.txt', 'link.txt')
        self.assertEqual(outside.read_text(encoding='utf-8'), 'Outside')

    def test_http_actions_route_to_file_operations(self):
        match = re.match(r'^/api/skills/([^/]+)$', '/api/skills/example')
        result = r_skills(self.conn, 'PATCH', match, {},
                          {'action': 'create', 'path': 'docs', 'kind': 'folder', 'tree_version': self.version()}, 'admin')
        self.assertIn('docs', result['directories'])
        result = r_skills(self.conn, 'PATCH', match, {},
                          {'action': 'move', 'source': 'docs', 'destination': 'references',
                           'tree_version': self.version()}, 'admin')
        self.assertIn('references', result['directories'])

    def test_orchestrator_can_manage_files_but_workers_cannot(self):
        api.upsert_agent(self.conn, 'lead', 'Lead', role='orchestrator')
        api.upsert_agent(self.conn, 'worker', 'Worker', role='expert')
        with patch.dict(os.environ, {'COMS_AGENT': 'lead'}):
            result = mcp.call_tool('skill_entry_create', {'slug': 'example', 'path': 'docs', 'kind': 'folder',
                                                         'tree_version': self.version()})
            self.assertFalse(result.get('isError'))
            mcp.call_tool('skill_entry_move', {'slug': 'example', 'source': 'docs', 'destination': 'references',
                                              'tree_version': self.version()})
        with patch.dict(os.environ, {'COMS_AGENT': 'worker'}):
            with self.assertRaises(api.BadRequest):
                mcp.call_tool('skill_entry_create', {'slug': 'example', 'path': 'bad', 'kind': 'folder',
                                                    'tree_version': self.version()})
        self.assertIn('references', api.get_skill(self.conn, 'example')['directories'])

    def test_workers_read_support_files_and_managers_write_them(self):
        api.upsert_agent(self.conn, 'lead', 'Lead', role='orchestrator')
        api.upsert_agent(self.conn, 'worker', 'Worker', role='expert')
        self.create('notes.txt', content='Supporting evidence')
        with patch.dict(os.environ, {'COMS_AGENT': 'worker'}):
            for options in ({}, {'collaboration': True}, {'dispatch_only': True}):
                result = mcp.call_tool('skill_file_get', {'slug': 'example', 'path': 'notes.txt'}, **options)
                self.assertIn('Supporting evidence', result['content'][0]['text'])
            with self.assertRaises(api.BadRequest):
                mcp.call_tool('skill_file_write', {'slug': 'example', 'path': 'notes.txt', 'content': 'Bad edit',
                                                  'version': skill_store.get_file('example', 'notes.txt')['version']})
        with patch.dict(os.environ, {'COMS_AGENT': 'lead'}):
            mcp.call_tool('skill_file_write', {'slug': 'example', 'path': 'notes.txt', 'content': 'Reviewed evidence',
                                              'version': skill_store.get_file('example', 'notes.txt')['version']})
        self.assertEqual(api.get_skill_file(self.conn, 'example', 'notes.txt')['content'], 'Reviewed evidence')
        api.set_skill_enabled(self.conn, 'example', False, 'admin')
        with patch.dict(os.environ, {'COMS_AGENT': 'worker'}), self.assertRaises(api.NotFound):
            mcp.call_tool('skill_file_get', {'slug': 'example', 'path': 'notes.txt'})

    def test_readonly_sessions_can_read_skills_without_updating_usage(self):
        self.create('notes.txt', content='Read-only evidence')
        result = mcp.call_tool('skill_get', {'slug': 'example'}, readonly=True)
        self.assertIn('Use supporting references.', result['content'][0]['text'])
        result = mcp.call_tool('skill_file_get', {'slug': 'example', 'path': 'notes.txt'}, readonly=True)
        self.assertIn('Read-only evidence', result['content'][0]['text'])
        self.assertEqual(api.get_skill(self.conn, 'example')['access_count'], 0)
