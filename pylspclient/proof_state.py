import os
import uuid
from pylspclient.lsp_structs import TextDocumentItem, TextDocumentIdentifier, VersionedTextDocumentIdentifier
from pylspclient.lsp_structs import TextDocumentContentChangeEvent, Position, ResponseError, ErrorCodes
from pylspclient.coq_lsp_structs import Step
from pylspclient.coq_lsp_client import CoqLspClient

class ProofState(object):
    def __init__(self, file_path, timeout=2):
        file_uri = f"file://{file_path}"
        self.coq_lsp_client = CoqLspClient(file_uri)
        self.coq_lsp_client.lsp_endpoint.timeout = timeout
        try:
            with open(file_path, 'r') as f:
                self.lines = f.read().split('\n')
                self.coq_lsp_client.didOpen(TextDocumentItem(file_uri, 'coq', 1, '\n'.join(self.lines)))
            self.path = file_path
            self.ast = self.coq_lsp_client.get_document(TextDocumentIdentifier(file_uri))
            self.ast = self.ast['spans']
            self.current_step = None
            self.in_proof = False
            self.__init_aux_file()
        except Exception as e:
            if not isinstance(e, ResponseError) or e.code != ErrorCodes.ServerQuit.value:
                self.coq_lsp_client.shutdown()
                self.coq_lsp_client.exit()
            raise e

    def __init_aux_file(self):
        dir = os.path.dirname(self.path)
        new_base_name = os.path.basename(self.path).split('.')
        new_base_name = new_base_name[0] + \
            f"new{str(uuid.uuid4()).replace('-', '')}." + new_base_name[1]
        self.aux_path = os.path.join(dir, new_base_name)
        self.aux_file_text = ''
        with open(self.aux_path, 'w'):
            # Create empty file
            pass

    def __get_expr(self, ast_step):
        if 'span' not in ast_step: return [None]
        return ast_step['span']['v']['expr']
    
    def __get_theorem_name(self, expr):
        return expr[2][0][0][0]['v'][1]

    def __write_on_aux_file(self, text):
        self.aux_file_text += text
        with open(self.aux_path, 'a') as f:
            f.write(text)
    
    def __call_didOpen(self):
        uri = f"file://{self.aux_path}"
        self.coq_lsp_client.didOpen(TextDocumentItem(uri, 'coq', 1, self.aux_file_text))

    def __command(self, keywords, search):
        for keyword in keywords:
            command = f'\n{keyword} {search}.'
            self.__write_on_aux_file(command)

    def __get_diagnostics(self, keywords, search, line):
        res = []
        uri = f"file://{self.aux_path}"
    
        for keyword in keywords:
            kw_res = None
            queries = self.coq_lsp_client.get_queries(TextDocumentIdentifier(uri), keyword)
            for query in queries:
                if query.query == f"{search}":
                    for result in query.results:
                        if result.range['start']['line'] == line:
                            kw_res = result.message
                            break
                    break
            res.append(kw_res)
            line += 1

        return tuple(res)
    
    def __compute(self, search, line):
        res_print, res_compute = self.__get_diagnostics(('Print', 'Compute'), f"{search}", line)

        if res_print is None: return None
        definition = res_print.split()
        if res_compute is None: return ' '.join(definition)
        theorem = res_compute.split()
        if theorem[1] == search and definition[1] != search:
            return ' '.join(theorem[1:])
        
        return ' '.join(definition)
    
    def __locate(self, search, line):
        nots = self.__get_diagnostics(('Locate',), f"\"{search}\"", line)[0].split('\n')
        fun = lambda x: x.endswith("(default interpretation)")
        if len(nots) > 1: return list(filter(fun, nots))[0][:-25]
        else: return nots[0][:-25] if fun(nots[0]) else nots[0]
    
    def __step_context(self):
        def transverse_ast(el):
            if isinstance(el, dict):
                res = []
                for k, v in el.items():
                    res.extend(transverse_ast(k))
                    res.extend(transverse_ast(v))
                return res
            elif isinstance(el, list) and len(el) == 3 and el[0] == 'Ser_Qualid':
                id = '.'.join([l[1] for l in el[1][1][::-1]] + [el[2][1]])
                first_line = len(self.aux_file_text.split('\n'))
                self.__command(("Print", "Compute"), id)
                return [(self.__compute, id, first_line)]
            elif isinstance(el, list) and len(el) == 4 and el[0] == 'CNotation':
                line = len(self.aux_file_text.split('\n'))
                self.__command(("Locate",), f"\"{el[2][1]}\"")
                return [(self.__locate, el[2][1], line)] + transverse_ast(el[1:])
            elif isinstance(el, list):
                res = []
                for v in el:
                    res.extend(transverse_ast(v))
                return res
            
            return []

        if not self.in_proof:
            return None

        res, transversed = [], transverse_ast(self.current_step)
        [res.append(x) for x in transversed if x not in res]
        return res

    def __step_goals(self):
        uri =  "file://" + self.path
        start_line = self.current_step['range']['end']['line']
        start_character = self.current_step['range']['end']['character']
        end_pos = Position(start_line, start_character)
        return self.coq_lsp_client.proof_goals(TextDocumentIdentifier(uri), end_pos)
    
    def __step_text(self, start_line, start_character):
        end_line = self.current_step['range']['end']['line']
        end_character = self.current_step['range']['end']['character']
        lines = self.lines[start_line:end_line + 1]
        lines[-1] = lines[-1][:end_character + 1]
        lines[0] = lines[0][start_character:]
        return '\n'.join(lines)

    def exec(self, steps=1):
        for _ in range(steps):
            if self.current_step != None:
                end_previous = (self.current_step['range']['end']['line'], 
                         self.current_step['range']['end']['character'])
            else:
                end_previous = (0, 0)
            self.current_step = self.ast.pop(0)
            if self.current_step is None: break

            text = self.__step_text(end_previous[0], end_previous[1])
            self.__write_on_aux_file(text)

            expr = self.__get_expr(self.current_step)
            if expr[0] == 'VernacProof' or (expr[0] == 'VernacExtend' and expr[1][0] == 'Obligations'):
                self.in_proof = True
            elif expr[0] == 'VernacEndProof':
                self.in_proof = False

    def __next_steps(self):
        if not self.in_proof:
            return None

        aux_steps = []
        while self.in_proof:
            goals = self.__step_goals()
            line = self.current_step['range']['end']['line']
            character = self.current_step['range']['end']['character']

            self.exec()
            context_calls = self.__step_context()
            text = self.__step_text(line, character)
            aux_steps.append((text, goals, context_calls))

        return aux_steps

    def get_current_theorem(self):
        expr = self.__get_expr(self.current_step)
        if expr[0] != 'VernacStartTheoremProof':
            return None
        name = self.__get_theorem_name(expr)
        return self.__compute(name)
    
    def jump_to_theorem(self):
        while self.__get_expr(self.current_step)[0] != 'VernacStartTheoremProof': 
            self.exec()
        return self.get_current_theorem()

    def jump_to_proof(self):
        while not self.in_proof and len(self.ast) > 0:
            self.exec()

    def proof_steps(self):
        aux_proofs = []
        while len(self.ast) > 0:
            self.exec()
            if self.in_proof:
                aux_proofs.append(self.__next_steps())
        
        proofs = []
        self.__call_didOpen()
        for aux_proof_steps in aux_proofs:
            proof_steps = []
            for step in aux_proof_steps:
                context = []
                if step[2] is None:
                    context = None
                else:
                    for context_call in step[2]:
                        context.append(context_call[0](*context_call[1:]))
                    filtered, context = filter(lambda x: x is not None, context), []
                    [context.append(x) for x in filtered if x not in context]
                proof_steps.append(Step(step[0], step[1], context))
            proofs.append(proof_steps)
        
        return proofs

    def is_invalid(self):
        uri = f"file://{self.path}"
        return self.coq_lsp_client.has_error(TextDocumentIdentifier(uri))

    def close(self):
        self.coq_lsp_client.shutdown()
        self.coq_lsp_client.exit()
        os.remove(self.aux_path)
