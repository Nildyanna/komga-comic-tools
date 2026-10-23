"""Tests for tagcommon.is_cbz. Run: python test_tagcommon.py"""
import os, sys, tempfile, unittest, zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tagcommon


class IsCbzTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def path(self, name, data=None):
        p = os.path.join(self.dir, name)
        if data is not None:
            open(p, "wb").write(data)
        return p

    def test_real_zip_is_zip_whatever_the_extension(self):
        z = self.path("a.cbr")
        zipfile.ZipFile(z, "w").writestr("x.jpg", b"1")
        self.assertTrue(tagcommon.is_cbz(z))

    def test_rar_named_cbz_is_not_zip(self):
        self.assertFalse(tagcommon.is_cbz(self.path("b.cbz", b"Rar!\x1a\x07\x00rest")))

    def test_unreadable_falls_back_to_extension(self):
        self.assertTrue(tagcommon.is_cbz(self.path("missing.cbz")))
        self.assertFalse(tagcommon.is_cbz(self.path("missing.cbr")))
        self.assertTrue(tagcommon.is_cbz(self.path("odd.cbz", b"????")))   # unknown header: trust the extension


if __name__ == "__main__":
    unittest.main()
