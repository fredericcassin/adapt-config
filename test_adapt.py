import unittest
import adapt 
 
class TestUM(unittest.TestCase):
 
    def setUp(self):
        pass
 
    def test_parser(self):
        parser = adapt.parse_args(['openwhisk.adapt.yaml', 'scan', '--quiet', '-o', r'c:\projects\src\openwhisk.scan.yml', r'c:\projects\src\incubator-openwhisk-deploy-kube-master.old/helm/openwhisk'])
        self.assertEqual(parser, parser)
 
if __name__ == '__main__':
    unittest.main()
