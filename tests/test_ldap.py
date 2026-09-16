"""The shared LDAPS helpers: argv shape (password by file only) and the LDIF reader."""

import base64
import unittest

from gideon.host.ldap import (
    LDAP_PASSWORD,
    NO_SUCH_OBJECT,
    bind_identity,
    filter_value,
    ldapsearch_argv,
    read_ldif,
)


class BindIdentity(unittest.TestCase):
    def test_bare_account_is_qualified_as_a_upn(self) -> None:
        self.assertEqual(bind_identity("svc-gideon-ldap", "example.org"), "svc-gideon-ldap@example.org")

    def test_upn_and_dn_forms_pass_through(self) -> None:
        self.assertEqual(bind_identity("svc@corp.example", "example.org"), "svc@corp.example")
        self.assertEqual(bind_identity("CN=svc,CN=Users,DC=x", "example.org"), "CN=svc,CN=Users,DC=x")


class Argv(unittest.TestCase):
    def test_password_travels_as_a_file_path_never_a_value(self) -> None:
        argv = ldapsearch_argv("example.org", 636, "svc-gideon-ldap", "DC=example,DC=org", "(objectClass=group)", ("dn",))
        self.assertEqual(argv[:2], ["env", "LDAPTLS_CACERT=/etc/gideon/ca.pem"])
        self.assertIn("-y", argv)
        self.assertEqual(argv[argv.index("-y") + 1], str(LDAP_PASSWORD))
        self.assertEqual(argv[argv.index("-H") + 1], "ldaps://example.org:636")
        self.assertEqual(argv[argv.index("-s") + 1], "sub")
        self.assertEqual(argv[-2:], ["(objectClass=group)", "dn"])

    def test_base_scope_is_selectable(self) -> None:
        argv = ldapsearch_argv("h", 636, "u", "CN=G,CN=Users,DC=h", "(objectClass=group)", scope="base")
        self.assertEqual(argv[argv.index("-s") + 1], "base")

    def test_no_such_object_is_the_ldap_result_code(self) -> None:
        self.assertEqual(NO_SUCH_OBJECT, 32)


class FilterValue(unittest.TestCase):
    def test_rfc_4515_escapes_the_five_special_characters(self) -> None:
        self.assertEqual(filter_value("CN=Last\\, First,OU=Groups (old)*,DC=x"), "CN=Last\\5c, First,OU=Groups \\28old\\29\\2a,DC=x")
        self.assertEqual(filter_value("plain-DN,OU=Fine"), "plain-DN,OU=Fine")
        self.assertEqual(filter_value("a\x00b"), "a\\00b")


class Ldif(unittest.TestCase):
    def test_folded_lines_base64_values_and_multiple_entries(self) -> None:
        encoded = base64.b64encode("Ünïcode Name".encode()).decode()
        text = (
            "version: 1\n"
            "# a comment\n"
            "dn: CN=alice,CN=Users,DC=example,DC=org\n"
            "sAMAccountName: alice\n"
            "mail: alice@example.org\n"
            "memberOf: CN=GIDEON-Users,CN=Users,DC=example,\n"
            " DC=org\n"
            "memberOf: CN=GIDEON-Admins,CN=Users,DC=example,DC=org\n"
            f"displayName:: {encoded}\n"
            "\n"
            "dn: CN=bob,CN=Users,DC=example,DC=org\n"
            "sAMAccountName: bob\n"
        )
        entries = read_ldif(text)
        self.assertEqual([entry.dn for entry in entries], ["CN=alice,CN=Users,DC=example,DC=org", "CN=bob,CN=Users,DC=example,DC=org"])
        alice = entries[0].attributes
        self.assertEqual(alice["samaccountname"], ("alice",))
        self.assertEqual(alice["mail"], ("alice@example.org",))
        self.assertEqual(
            alice["memberof"],
            ("CN=GIDEON-Users,CN=Users,DC=example,DC=org", "CN=GIDEON-Admins,CN=Users,DC=example,DC=org"),
        )
        self.assertEqual(alice["displayname"], ("Ünïcode Name",))
        self.assertNotIn("mail", entries[1].attributes)

    def test_empty_output_has_no_entries(self) -> None:
        self.assertEqual(read_ldif(""), ())
        self.assertEqual(read_ldif("\n\n"), ())

    def test_entry_without_dn_is_ignored(self) -> None:
        self.assertEqual(read_ldif("cn: orphan\n"), ())
