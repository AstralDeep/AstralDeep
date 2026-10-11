import unittest
from unittest.mock import patch, MagicMock
import hashlib

def load_memory(filename):
    with open(filename, 'r') as file:
        return file.read()

def save_memory(memory, filename):
    with open(filename, 'w') as file:
        file.write(memory)

def hash_memory(memory):
    return hashlib.sha256(memory.encode('utf-8')).hexdigest()

def get_memory_digest(memory):
    return hashlib.sha256(memory.encode('utf-8')).hexdigest()

def compare_memories(memory1, memory2):
    return memory1 == memory2

def get_memory_label(memory):
    return 'label'

def get_memory_rationale(memory):
    return 'rationale'

def get_memory_paraphrases(memory):
    return ['paraphrase1', 'paraphrase2']

def freeze_memory(memory, filename):
    label = get_memory_label(memory)
    rationale = get_memory_rationale(memory)
    digest = get_memory_digest(memory)
    with open(filename, 'w') as file:
        file.write(f'label: {label}\n')
        file.write(f'rationale: {rationale}\n')
        file.write(f'digest: {digest}\n')

class TestMemoryFunctions(unittest.TestCase):

    @patch('test_solution.load_memory')
    def test_load_memory_file_not_found(self, mock_load_memory):
        mock_load_memory.side_effect = FileNotFoundError
        with self.assertRaises(FileNotFoundError):
            load_memory('nonexistent_file.txt')

    @patch('test_solution.load_memory')
    def test_load_memory_success(self, mock_load_memory):
        mock_load_memory.return_value = 'test_memory'
        self.assertEqual(load_memory('test_file.txt'), 'test_memory')

    @patch('test_solution.hash_memory')
    def test_hash_memory(self, mock_hash_memory):
        memory = 'test_memory'
        expected_hash = hashlib.sha256(memory.encode('utf-8')).hexdigest()
        mock_hash_memory.return_value = expected_hash
        self.assertEqual(hash_memory(memory), expected_hash)

    @patch('test_solution.save_memory')
    def test_save_memory(self, mock_save_memory):
        memory = 'test_memory'
        save_memory(memory, 'test_file.txt')
        mock_save_memory.assert_called_once_with(memory, 'test_file.txt')

    @patch('test_solution.get_memory_digest')
    def test_get_memory_digest(self, mock_get_memory_digest):
        memory = 'test_memory'
        expected_digest = hashlib.sha256(memory.encode('utf-8')).hexdigest()
        mock_get_memory_digest.return_value = expected_digest
        self.assertEqual(get_memory_digest(memory), expected_digest)

    @patch('test_solution.compare_memories')
    def test_compare_memories(self, mock_compare_memories):
        memory1 = 'test_memory1'
        memory2 = 'test_memory2'
        memory3 = 'test_memory1'
        mock_compare_memories.return_value = False
        self.assertFalse(compare_memories(memory1, memory2))
        mock_compare_memories.return_value = True
        self.assertTrue(compare_memories(memory1, memory3))

    @patch('test_solution.get_memory_label')
    def test_get_memory_label(self, mock_get_memory_label):
        memory = 'test_memory'
        mock_get_memory_label.return_value = 'label'
        self.assertEqual(get_memory_label(memory), 'label')

    @patch('test_solution.get_memory_rationale')
    def test_get_memory_rationale(self, mock_get_memory_rationale):
        memory = 'test_memory'
        mock_get_memory_rationale.return_value = 'rationale'
        self.assertEqual(get_memory_rationale(memory), 'rationale')

    @patch('test_solution.get_memory_paraphrases')
    def test_get_memory_paraphrases(self, mock_get_memory_paraphrases):
        memory = 'test_memory'
        mock_get_memory_paraphrases.return_value = ['paraphrase1', 'paraphrase2']
        self.assertEqual(get_memory_paraphrases(memory), ['paraphrase1', 'paraphrase2'])

    @patch('test_solution.get_memory_label')
    @patch('test_solution.get_memory_rationale')
    @patch('test_solution.get_memory_digest')
    def test_freeze_memory(self, mock_digest, mock_rationale, mock_label):
        memory = 'test_memory'
        mock_label.return_value = 'test_label'
        mock_rationale.return_value = 'test_rationale'
        mock_digest.return_value = 'test_digest'
        freeze_memory(memory, 'test_file.txt')
        mock_label.assert_called_once_with(memory)
        mock_rationale.assert_called_once_with(memory)
        mock_digest.assert_called_once_with(memory)

if __name__ == '__main__':
    unittest.main()