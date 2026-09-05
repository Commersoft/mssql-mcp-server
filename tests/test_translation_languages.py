import json
import unittest
from unittest.mock import MagicMock, patch

from mssql_mcp_server.cs_tools import _core, ng_window
from mssql_mcp_server.cs_tools.translations import audit_ui_translations


class TranslationLanguagesTests(unittest.TestCase):
    def test_translation_creation_preserves_czech_croatian_and_hungarian(self):
        cursor = MagicMock()
        with patch.object(_core, '_exec_scalar', return_value=None), patch.object(_core, '_jsonsave', return_value=None) as save:
            _core._ensure_translate(cursor, {'PL': 'Zapisz', 'EN': 'Save', 'CZ': 'Uložit', 'HR': 'Spremi', 'HU': 'Mentés'})
        self.assertEqual(save.call_args.args[2][0]['Content_CZ'], 'Uložit')
        self.assertEqual(save.call_args.args[2][0]['Content_HR'], 'Spremi')
        self.assertEqual(save.call_args.args[2][0]['Content_HU'], 'Mentés')

    def test_window_translation_registration_preserves_new_languages(self):
        cursor = MagicMock()
        conn = MagicMock()
        conn.__enter__.return_value.cursor.return_value.__enter__.return_value = cursor
        with patch.object(ng_window, 'connect', return_value=conn), patch.object(ng_window, '_exec_scalar', return_value=None), patch.object(ng_window, '_jsonsave', return_value=None) as save:
            response = ng_window.ng_register_translates('unused', 'csERPMain', [{'ident': 'SAVE', 'PL': 'Zapisz', 'EN': 'Save', 'CZ': 'Uložit', 'HR': 'Spremi'}])
        self.assertTrue(response.startswith('OK:'))
        self.assertEqual(save.call_args_list[0].args[2][0]['Content_CZ'], 'Uložit')
        self.assertEqual(save.call_args_list[0].args[2][0]['Content_HR'], 'Spremi')

    def test_audit_rejects_untrusted_identifiers_before_connecting(self):
        with patch('mssql_mcp_server.cs_tools.translations.connect') as connect:
            self.assertTrue(audit_ui_translations('unused', table_name='csUsr; delete').startswith('Error:'))
            self.assertTrue(audit_ui_translations('unused', sample_limit=101).startswith('Error:'))
            self.assertTrue(audit_ui_translations('unused', languages='PL').startswith('Error:'))
        connect.assert_not_called()

    def test_reusing_translation_fills_gaps_without_erasing_existing_content(self):
        row = {'csTranslateId': 42, 'csTranslateG': 'EXISTING', 'Content_PL': 'Zapisz',
               'Content_EN': 'Save', 'Content_CZ': ' ', 'Content_HR': 'Spremi', 'IsActive': 1}
        with patch.object(_core, '_exec_scalar', side_effect=['EXISTING', json.dumps(row)]), patch.object(_core, '_jsonsave', return_value=None) as save:
            _core._ensure_translate(MagicMock(), {'PL': 'Zapisz', 'EN': 'Save', 'CZ': 'Uložit', 'HR': 'Changed'})
        saved = save.call_args.args[2][0]
        self.assertEqual(saved['csTranslateId'], 42)
        self.assertEqual(saved['csTranslateG'], 'EXISTING')
        self.assertEqual(saved['Content_CZ'], 'Uložit')
        self.assertEqual(saved['Content_HR'], 'Spremi')
        self.assertEqual(saved['IsActive'], 1)
        self.assertEqual(saved['_opr'], 'U')
        with patch.object(_core, '_exec_scalar', return_value=json.dumps(saved)), patch.object(_core, '_jsonsave') as save_again:
            self.assertEqual(_core._fill_translate_gaps(MagicMock(), 'EXISTING', {'CZ': 'Uložit'}), 0)
        save_again.assert_not_called()

    def test_registering_existing_link_supplies_both_id_and_guid(self):
        conn = MagicMock()
        with patch.object(ng_window, 'connect', return_value=conn), patch.object(ng_window, '_exec_scalar', side_effect=['EXISTING-LINK', 7]), patch.object(ng_window, '_jsonsave', return_value=None) as save:
            response = ng_window.ng_register_translates('unused', 'csERPMain', [{'ident': 'SAVE', 'cs_translate_g': 'EXISTING-TEXT'}])
        self.assertTrue(response.startswith('OK:'))
        saved = save.call_args.args[2][0]
        self.assertEqual(saved['csNGAppWindowTranslatesId'], 7)
        self.assertEqual(saved['csNGAppWindowTranslatesG'], 'EXISTING-LINK')
        self.assertEqual(saved['_opr'], 'U')

    def test_invalid_language_in_later_item_prevents_partial_registration(self):
        with patch.object(ng_window, 'connect') as connect:
            result = ng_window.ng_register_translates('unused', 'csERPMain', [
                {'ident': 'SAVE', 'PL': 'Zapisz'}, {'ident': 'CLOSE', 'XX': 'Unsupported'}])
        self.assertTrue(result.startswith('Error: unknown translation keys'))
        connect.assert_not_called()

    def test_audit_distinguishes_missing_columns_and_empty_translations(self):
        cursor = MagicMock()
        cursor.execute.return_value.fetchall.side_effect = [
            [('PL',), ('CZ',), ('HR',)],
            [('csNGAppWindowsId',), ('csNGAppWindowsG',), ('appWindowDesc_PL',), ('appWindowDesc_CZ',)],
        ]
        cursor.execute.return_value.fetchone.return_value = (3, 0, 2)
        conn = MagicMock()
        conn.__enter__.return_value.cursor.return_value.__enter__.return_value = cursor
        with patch('mssql_mcp_server.cs_tools.translations.connect', return_value=conn):
            result = json.loads(audit_ui_translations('unused', table_name='csNGAppWindows', sample_limit=0))
        self.assertEqual(result['missing_cells'], 2)
        self.assertEqual(result['coverage'][0]['missing'], {'PL': 0, 'CZ': 2})
        self.assertEqual(result['missing_columns'][0]['language'], 'HR')
        for args in cursor.execute.call_args_list:
            self.assertIn('with(nolock)', args.args[0])


if __name__ == '__main__':
    unittest.main()
