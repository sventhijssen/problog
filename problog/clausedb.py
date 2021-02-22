import os
from collections import defaultdict, namedtuple

from .errors import InvalidValue
from .logic import *
from .program import LogicProgram, PrologFile
from .util import OrderedSet


class ClauseDB(LogicProgram):
    """Compiled logic program.

    A logic program is compiled into a table of instructions.
    The types of instructions are:

    define( functor, arity, defs )
        Pointer to all definitions of functor/arity.
        Definitions can be: ``fact``, ``clause`` or ``adc``.

    clause( functor, arguments, bodynode, varcount )
        Single clause. Functor is the head functor, Arguments are the head arguments. Body node is a pointer to the node representing the body. Var count is the number of variables in head and body.

    fact( functor, arguments, probability )
        Single fact.

    adc( functor, arguments, bodynode, varcount, parent )
        Single annotated disjunction choice. Fields have same meaning as with ``clause``, parent_node points to the parent ``ad`` node.

    ad( childnodes )
        Annotated disjunction group. Child nodes point to the ``adc`` nodes of the clause.

    call( functor, arguments, defnode )
        Body literal with call to clause or builtin. Arguments contains the call arguments, definition node is the pointer to the definition node of the given functor/arity.

    conj( childnodes )
        Logical and. Currently, only 2 children are supported.

    disj( childnodes )
        Logical or. Currently, only 2 children are supported.

    neg( childnode )
        Logical not.

    """

    _define = namedtuple("define", ("functor", "arity", "children", "location"))
    _clause = namedtuple(
        "clause",
        (
            "functor",
            "args",
            "probability",
            "child",
            "varcount",
            "locvars",
            "group",
            "location",
        ),
    )
    _fact = namedtuple("fact", ("functor", "args", "probability", "location"))
    _call = namedtuple(
        "call", ("functor", "args", "defnode", "location", "op_priority", "op_spec")
    )
    _disj = namedtuple("disj", ("children", "location"))
    _conj = namedtuple("conj", ("children", "location"))
    _neg = namedtuple("neg", ("child", "location"))
    _choice = namedtuple(
        "choice",
        ("functor", "args", "probability", "locvars", "group", "choice", "location"),
    )
    _extern = namedtuple("extern", ("functor", "arity", "function"))

    FUNCTOR_CHOICE = "choice"
    FUNCTOR_BODY = "body"

    def __init__(self, builtins=None, parent=None):
        LogicProgram.__init__(self)
        self.__nodes = []  # list of nodes
        self.__heads = {}  # head.sig => node index

        self.__builtins = builtins

        self.data = {}
        self.engine = None

        self.__parent = parent
        self.__node_redirect = {}
        self.__extern = defaultdict(list)

        if parent is None:
            self.__offset = 0
        else:
            if hasattr(parent, "line_info"):
                self.line_info = parent.line_info
            if hasattr(parent, "source_files"):
                self.source_files = parent.source_files[:]
            self.__offset = len(parent)

        self.dont_cache = set()

        self.queries = []
        self._load_builtin_module()

    def _load_builtin_module(self):
        # self.use_module(Term('library', Term('builtin')), None)
        pass

    def __len__(self):
        return len(self.__nodes) + self.__offset

    def extend(self):
        return ClauseDB(parent=self, builtins=self.__builtins)

    def set_data(self, key, value):
        self.data[key] = value

    def update_data(self, key, value):
        if self.has_data(key):
            if type(value) == list:
                self.data[key] += value
            elif type(value) == dict:
                self.data[key].update(value)
            else:
                raise TypeError("Can't update data of type '%s'" % type(value))
        else:
            self.data[key] = value

    def has_data(self, key):
        return key in self.data

    def get_data(self, key, default=None):
        return self.data.get(key, default)

    def get_builtin(self, signature):
        if self.__builtins is None:
            if self.__parent is not None:
                return self.__parent.get_builtin(signature)
            else:
                return None
        else:
            return self.__builtins.get(signature)

    def get_reserved_names(self):
        return {self.FUNCTOR_CHOICE, self.FUNCTOR_BODY}

    def is_reserved_name(self, name):
        return name is self.get_reserved_names()

    def _create_index(self, arity):
        # return []
        return ClauseIndex(self, arity)

    def _add_and_node(self, op1, op2, location=None):
        """Add an *and* node."""
        return self._append_node(self._conj((op1, op2), location))

    def _add_not_node(self, op1, location=None):
        """Add a *not* node."""
        return self._append_node(self._neg(op1, location))

    def _add_or_node(self, op1, op2, location=None):
        """Add an *or* node."""
        return self._append_node(self._disj((op1, op2), location))

    def _scope_term(self, term, scope, is_problog_scope=False):
        if term.signature in self.__builtins:
            scope = None
        if term.functor == "_directive":
            scope = None
        if scope is not None:
            if not is_problog_scope:
                term.functor = "_%s_%s" % (
                    scope,
                    term.functor,
                )  # _scope_functor --> scope:functor
            else:
                ## CG MODIFS
                new_term = Term(
                    "':'", scope, term.with_probability(None), p=term.probability
                )
                return new_term
        return term

    def _add_define_node(self, head, childnode):
        define_index = self._add_head(head)
        define_node = self.get_node(define_index)
        if not define_node:
            clauses = self._create_index(head.arity)
            self._set_node(
                define_index,
                self._define(head.functor, head.arity, clauses, head.location),
            )
        else:
            clauses = define_node.children
        clauses.append(childnode)
        return childnode

    def _add_choice_node(
        self,
        choice,
        functor,
        args,
        probability,
        locvars,
        group,
        location=None,
        scope=None,
    ):
        choice_node = self._append_node(
            self._choice(functor, args, probability, locvars, group, choice, location)
        )
        return choice_node

    def _add_clause_node(self, head, body, varcount, locvars, group=None):
        clause_node = self._append_node(
            self._clause(
                head.functor,
                head.args,
                head.probability,
                body,
                varcount,
                locvars,
                group,
                head.location,
            )
        )
        return self._add_define_node(head, clause_node)

    def _add_call_node(self, term, scope=None, is_problog_scope=False):
        """Add a *call* node."""
        # if term.signature in ('query/1', 'evidence/1', 'evidence/2'):
        #    raise AccessError("Can\'t call %s directly." % term.signature)

        term = self._scope_term(term, scope, is_problog_scope=is_problog_scope)
        defnode = self._add_head(term, create=False)
        return self._append_node(
            self._call(
                term.functor,
                term.args,
                defnode,
                term.location,
                term.op_priority,
                term.op_spec,
            )
        )

    def get_node(self, index):
        """Get the instruction node at the given index.

        :param index: index of the node to retrieve
        :type index: :class:`int`
        :returns: requested node
        :rtype: :class:`tuple`
        :raises IndexError: the given index does not point to a node
        """
        index = self.__node_redirect.get(index, index)

        if index < self.__offset:
            return self.__parent.get_node(index)
        else:
            return self.__nodes[index - self.__offset]

    def _set_node(self, index, node):
        if index < self.__offset:
            raise IndexError("Can't update node in parent.")
        else:
            self.__nodes[index - self.__offset] = node

    def _append_node(self, node=()):
        index = len(self)
        self.__nodes.append(node)
        return index

    def _get_head(self, head=None):
        node = self.__heads.get(head.signature)
        if node is None and self.__parent:
            node = self.__parent._get_head(head)
        return node

    def _set_head(self, head, index):
        self.__heads[head.signature] = index

    def _add_head(self, head, create=True):
        if self.is_reserved_name(head.functor):
            raise AccessError("'%s' is a reserved name" % head.functor)
        node = self.get_builtin(head.signature)
        if node is not None:
            if create:
                raise AccessError("Can not overwrite built-in '%s'." % head.signature)
            else:
                return node

        node = self._get_head(head)
        if node is None:
            if create:
                node = self._append_node(
                    self._define(
                        head.functor,
                        head.arity,
                        self._create_index(head.arity),
                        head.location,
                    )
                )
            else:
                node = self._append_node()
            self._set_head(head, node)
        elif create and node < self.__offset:
            existing = self.get_node(node)
            # node exists in parent
            clauses = self._create_index(head.arity)
            if existing:
                for c in existing.children:
                    clauses.append(c)
            old_node = node
            node = self._append_node(
                self._define(head.functor, head.arity, clauses, head.location)
            )
            self.__node_redirect[old_node] = node
            self._set_head(head, node)

        return node

    def find(self, head):
        """Find the ``define`` node corresponding to the given head.

        :param head: clause head to match
        :type head: :class:`.basic.Term`
        :returns: location of the clause node in the database, \
                     returns ``None`` if no such node exists
        :rtype: :class:`int` or ``None``
        """
        return self._get_head(head)

    def __repr__(self):
        s = ""
        for i, n in enumerate(self.__nodes):
            i += self.__offset
            s += "%s: %s\n" % (i, n)
        s += str(self.__heads)
        s += "\n"
        s += "Redirects: " + str(self.__node_redirect)
        return s

    def add_clause(self, clause, scope=None, is_problog_scope=False):
        """Add a clause to the database.

       :param clause: Clause to add
       :type clause: Clause
       :returns: location of the definition node in the database
       :rtype: int
        """
        return self._compile(clause, scope=scope, is_problog_scope=is_problog_scope)

    def add_fact(self, term, scope=None, is_problog_scope=False):
        """Add a fact to the database.
       :param term: fact to add
       :type term: Term
       :return: position of the definition node in the database
       :rtype: int
        """
        # Count the number of variables in the fact
        variables = _AutoDict()
        term.apply(variables)
        # If the fact has variables, threat is as a clause.
        if len(variables) == 0:
            term = self._scope_term(term, scope, is_problog_scope=is_problog_scope)
            fact_node = self._append_node(
                self._fact(term.functor, term.args, term.probability, term.location)
            )
            return self._add_define_node(term, fact_node)
        else:
            return self.add_clause(
                Clause(term, Term("true")),
                scope=scope,
                is_problog_scope=is_problog_scope,
            )

    def add_extern(self, predicate, arity, func, scope=None, is_problog_scope=False):
        head = Term(predicate, *[None] * arity)
        head = self._scope_term(head, scope, is_problog_scope=is_problog_scope)
        node_id = self._get_head(head)
        ext = self._extern(head.functor, head.arity, func)
        if node_id is None:
            node_id = self._append_node(ext)
            self._set_head(head, node_id)
        else:
            node = self.get_node(node_id)
            if node == ():
                self._set_node(node_id, ext)
            else:
                node_id = self._append_node(ext)
                self._add_define_node(head, node_id)
        self.__extern[scope].append(Term("'/'", Term(predicate), Constant(arity)))

    def get_local_scope(self, signature):
        if signature in ("findall/3", "all/3", "all_or_none/3"):
            return 0, 1
        else:
            return []

    def _compile(self, struct, variables=None, scope=None, is_problog_scope=False):
        """Compile the given structure and add it to the database.

        :param struct: structure to compile
        :type struct: Term
        :param variables: mapping between variable names and variable index
        :type variables: _AutoDict
        :return: position of the compiled structure in the database
        :rtype: int
        """
        if variables is None:
            variables = _AutoDict()

        if isinstance(struct, And):
            op1 = self._compile(
                struct.op1, variables, scope=scope, is_problog_scope=is_problog_scope
            )
            op2 = self._compile(
                struct.op2, variables, scope=scope, is_problog_scope=is_problog_scope
            )
            return self._add_and_node(op1, op2)
        elif isinstance(struct, Or):
            op1 = self._compile(
                struct.op1, variables, scope=scope, is_problog_scope=is_problog_scope
            )
            op2 = self._compile(
                struct.op2, variables, scope=scope, is_problog_scope=is_problog_scope
            )
            return self._add_or_node(op1, op2)
        elif isinstance(struct, Not):
            variables.enter_local()
            child = self._compile(
                struct.child, variables, scope=scope, is_problog_scope=is_problog_scope
            )
            variables.exit_local()
            return self._add_not_node(child, location=struct.location)
        elif isinstance(struct, Term) and struct.signature == "not/1":
            child = self._compile(
                struct.args[0],
                variables,
                scope=scope,
                is_problog_scope=is_problog_scope,
            )
            return self._add_not_node(child, location=struct.location)
        elif isinstance(struct, AnnotatedDisjunction):
            # Determine number of variables in the head
            new_heads = [head.apply(variables) for head in struct.heads]

            # Group id
            group = len(self.__nodes)

            # Create the body clause
            body_node = self._compile(
                struct.body, variables, scope=scope, is_problog_scope=is_problog_scope
            )
            body_count = len(variables)
            # Body arguments
            body_args = tuple(range(0, len(variables)))
            body_functor = self.FUNCTOR_BODY + "_" + str(len(self))
            if len(new_heads) > 1:
                heads_list = Term("multi")  # list2term(new_heads)
            else:
                heads_list = new_heads[0].with_probability(None)
            body_head = Term(body_functor, Constant(group), heads_list, *body_args)
            self._add_clause_node(
                body_head, body_node, len(variables), variables.local_variables
            )
            clause_body = self._add_head(body_head)
            for choice, head in enumerate(new_heads):
                head = self._scope_term(head, scope, is_problog_scope=is_problog_scope)
                # For each head: add choice node
                choice_functor = Term(
                    self.FUNCTOR_CHOICE,
                    Constant(group),
                    Constant(choice),
                    head.with_probability(),
                )
                choice_node = self._add_choice_node(
                    choice,
                    choice_functor,
                    body_args,
                    head.probability,
                    variables.local_variables,
                    group,
                    head.location,
                )
                choice_call = self._append_node(
                    self._call(
                        choice_functor,
                        body_args,
                        choice_node,
                        head.location,
                        None,
                        None,
                    )
                )
                body_call = self._append_node(
                    self._call(
                        body_functor,
                        body_head.args,
                        clause_body,
                        head.location,
                        None,
                        None,
                    )
                )
                choice_body = self._add_and_node(body_call, choice_call)
                self._add_clause_node(head, choice_body, body_count, {}, group=group)
            return None
        elif isinstance(struct, Clause):
            if struct.head.probability is not None:
                return self._compile(
                    AnnotatedDisjunction([struct.head], struct.body),
                    scope=scope,
                    is_problog_scope=is_problog_scope,
                )
            else:
                new_head = self._scope_term(
                    struct.head.apply(variables),
                    scope,
                    is_problog_scope=is_problog_scope,
                )
                body_node = self._compile(
                    struct.body,
                    variables,
                    scope=scope,
                    is_problog_scope=is_problog_scope,
                )
                return self._add_clause_node(
                    new_head, body_node, len(variables), variables.local_variables
                )
        elif isinstance(struct, Var):
            return self._add_call_node(
                Term("call", struct.apply(variables), location=struct.location),
                scope=scope,
                is_problog_scope=is_problog_scope,
            )
        elif isinstance(struct, Term):
            local_scope = self.get_local_scope(struct.signature)
            if local_scope:
                # Special case for findall: any variables added by the first
                #  two arguments of findall are 'local' variables.
                args = []
                for i, a in enumerate(struct.args):
                    if not isinstance(a, Term):
                        # For nested findalls: 'a' can be a raw variable pointer
                        # Temporarily wrap it in a Term, so we can call 'apply' on it.
                        a = Term("_", a)
                    if i in local_scope:
                        variables.enter_local()
                        new_arg = a.apply(variables)
                        variables.exit_local()
                    else:
                        new_arg = a.apply(variables)
                    if a.functor == "_":
                        # If the argument was temporarily wrapped: unwrap it.
                        new_arg = new_arg.args[0]
                    args.append(new_arg)
                return self._add_call_node(
                    struct(*args), scope=scope, is_problog_scope=is_problog_scope
                )
            elif struct.functor in ("consult", "use_module"):
                new_struct = Term(
                    "_" + struct.functor,
                    Term(scope),
                    *struct.args,
                    location=struct.location
                )
                return self._add_call_node(
                    new_struct.apply(variables),
                    scope=scope,
                    is_problog_scope=is_problog_scope,
                )
            else:
                return self._add_call_node(
                    struct.apply(variables),
                    scope=scope,
                    is_problog_scope=is_problog_scope,
                )
        else:
            raise ValueError("Unknown structure type: '%s'" % struct)

    def _create_vars(self, term):
        if type(term) == int:
            return Var("V_" + str(term))
        else:
            args = [self._create_vars(arg) for arg in term.args]
            term = term.with_args(*args)
            if term.probability is not None:
                term = term.with_probability(self._create_vars(term.probability))
            return term

    def _extract(self, node_id):
        node = self.get_node(node_id)
        if not node:
            raise ValueError("Unexpected empty node.")

        nodetype = type(node).__name__
        if nodetype == "fact":
            return Term(node.functor, *node.args, p=node.probability)
        elif nodetype == "call":
            func = node.functor
            args = node.args
            if isinstance(func, Term):
                return self._create_vars(func(*(func.args + args)))
            else:
                return self._create_vars(
                    Term(func, *args, priority=node.op_priority, opspec=node.op_spec)
                )
        elif nodetype == "conj":
            a, b = node.children
            return And(self._extract(a), self._extract(b))
        elif nodetype == "disj":
            a, b = node.children
            return Or(self._extract(a), self._extract(b))
        elif nodetype == "neg":
            return Not("\\+", self._extract(node.child))
        else:
            raise ValueError("Unknown node type: '%s'" % nodetype)

    def to_clause(self, index):
        node = self.get_node(index)
        nodetype = type(node).__name__
        if nodetype == "fact":
            return Term(node.functor, *node.args, p=node.probability)
        elif nodetype == "clause":
            head = self._create_vars(Term(node.functor, *node.args, p=node.probability))
            return Clause(head, self._extract(node.child))

    def __iter__(self):
        clause_groups = defaultdict(list)
        for index, node in self.enum_nodes():
            if not node:
                continue
            nodetype = type(node).__name__
            if nodetype == "fact":
                yield Term(node.functor, *node.args, p=node.probability)
            elif nodetype == "clause":
                if node.group is None:
                    head = self._create_vars(
                        Term(node.functor, *node.args, p=node.probability)
                    )
                    yield Clause(head, self._extract(node.child))
                else:
                    clause_groups[node.group].append(index)
        for group in clause_groups.values():
            heads = []
            body = None
            for index in group:
                node = self.get_node(index)
                heads.append(
                    self._create_vars(
                        Term(node.functor, *node.args, p=node.probability)
                    )
                )
                if body is None:
                    body_node = self.get_node(node.child)
                    body_node = self.get_node(body_node.children[0])
                    body = self._create_vars(Term(body_node.functor, *body_node.args))
            yield AnnotatedDisjunction(heads, body)

    def iter_raw(self):
        """Iterate over clauses of model as represented in the database i.e. with choice facts and
         without annotated disjunctions.
        """

        clause_groups = defaultdict(list)
        for index, node in self.enum_nodes():
            if not node:
                continue
            nodetype = type(node).__name__
            if nodetype == "fact":
                yield Term(node.functor, *node.args, p=node.probability)
            elif nodetype == "clause":
                if node.group is None:
                    head = self._create_vars(
                        Term(node.functor, *node.args, p=node.probability)
                    )
                    yield Clause(head, self._extract(node.child))
                else:
                    head = self._create_vars(Term(node.functor, *node.args))
                    yield Clause(head, self._extract(node.child))
            elif nodetype == "choice":
                group = node.functor.args[0]
                c = node.functor(*(node.functor.args + node.args))
                clause_groups[group].append(c)
                yield c.with_probability(node.probability)

        for group in clause_groups.values():
            if len(group) > 1:
                yield Term("mutual_exclusive", list2term(group))

    def resolve_filename(self, filename):
        if (
            hasattr(filename, "functor")
            and filename.functor == "library"
            and filename.arity == 1
        ):
            from . import library_paths

            libname = unquote(str(filename.args[0]))
            for path in library_paths:
                filename = os.path.join(path, libname)
                if os.path.exists(filename):
                    return filename
                elif os.path.exists(filename + ".pl"):
                    return filename + ".pl"
                elif os.path.exists(filename + ".py"):
                    return filename + ".py"
        else:
            root = self.source_root
            if hasattr(filename, "location") and filename.location:
                source_root = self.source_files[filename.location[0]]
                if source_root:
                    root = os.path.dirname(source_root)

            filename = os.path.join(root, unquote(str(filename)))
            if os.path.exists(filename):
                return filename
            elif os.path.exists(filename + ".pl"):
                return filename + ".pl"
            elif os.path.exists(filename + ".py"):
                return filename + ".py"
        return filename

    def create_function(self, functor, arity):
        """Create a Python function that can be used to query a specific predicate on this database.

        :param functor: functor of the predicate
        :param arity: arity of the predicate (the function will take arity - 1 arguments
        :return: a Python callable
        """
        return PrologFunction(self, functor, arity)

    def enum_nodes(self):
        if self.__parent:
            for i, n in self.__parent.enum_nodes():
                yield i, n
        for i, n in enumerate(self.__nodes):
            i += self.__offset
            yield i, n

    def iter_nodes(self):
        if self.__parent:
            for n in self.__parent.iter_nodes():
                yield n
        for n in self.__nodes:
            yield n

    def consult(self, filename, location=None, my_scope=None):
        filename = self.resolve_filename(filename)
        if filename is None:
            raise ConsultError(
                message="Consult: file not found '%s'" % filename,
                location=self.lineno(location),
            )

        # Prevent loading the same file twice
        if filename not in self.source_files:
            identifier = len(self.source_files)
            self.source_files.append(filename)
            self.source_parent.append(location)
            program = PrologFile(
                filename,
                identifier=identifier,
                factory=self.extra_info.get("factory"),
                parser=self.extra_info.get("parser"),
            )
            self.line_info.append(program.line_info[0])
            # engine._process_directives(database)
            return self.add_all(program)
        else:
            return None, None

    def add_all(self, program):
        module_name = None
        module_preds = None
        for index, clause in enumerate(program):
            if (
                clause.functor == ":-"
                and hasattr(clause.args[0], "functor")
                and clause.args[0].functor == "_directive"
                and clause.args[1].signature == "module/2"
            ):
                if index > 0:
                    raise AccessError(
                        "'module' directive should appear at top of module"
                    )
                module_name = str(clause.args[1].args[0])
                module_preds = clause.args[1].args[1]
            else:
                self.add_statement(clause, module_name, False)
        if module_preds is not None:
            module_preds = term2list(module_preds)
        return module_name, module_preds

    def use_module(self, filename, predicates, location=None, my_scope=None):
        filename = self.resolve_filename(filename)
        if filename is None:
            raise ConsultError("Unknown library location", self.lineno(location))
        elif filename is not None and filename[-3:] == ".py":
            try:
                module_name, module_predicates = self.load_external_module(filename)
            except IOError as err:
                raise ConsultError(
                    "Error while reading external library: %s" % str(err),
                    self.lineno(location),
                )
        else:
            module_name, module_predicates = self.consult(
                Term(filename), location=location
            )

        if module_name is not None:
            if predicates is None:
                for mp in module_predicates:
                    self._create_alias(mp, module_name, my_scope=my_scope)
            elif predicates.functor == "except":
                preds = set(term2list(predicates.args[0]))
                for mp in module_predicates:
                    if mp not in preds:
                        self._create_alias(mp, module_name, my_scope=my_scope)
            else:
                preds = {}
                for pred in term2list(predicates):
                    if pred.functor == "'as'":
                        mp = pred.args[0]
                        rename = pred.args[1]
                    else:
                        mp = pred
                        rename = pred.args[0]
                    if mp in module_predicates:
                        self._create_alias(
                            mp, module_name, rename=rename, my_scope=my_scope
                        )
                    else:
                        raise GroundingError(
                            "Imported predicate %s not defined in module %s"
                            % (mp, module_name),
                            location=location,
                        )

    def _create_alias(
        self, pred, scope, rename=None, my_scope=None, is_problog_scope=False
    ):
        if rename is None:
            rename = pred.args[0]

        if scope is not None:
            root_sign = self._scope_term(
                Term(rename, *[None] * int(pred.args[1])),
                my_scope,
                is_problog_scope=is_problog_scope,
            )
            scoped_sign = self._scope_term(
                Term(pred.args[0], *[None] * int(pred.args[1])),
                scope,
                is_problog_scope=is_problog_scope,
            )

            rh = self._add_head(root_sign, create=False)
            sh = self._add_head(scoped_sign, create=False)

            if self.get_node(rh):
                # TODO warning that user code overrides library
                pass
            elif rh is not None:
                sh = self.__node_redirect.get(sh, sh)
                self.__node_redirect[rh] = sh

    def load_external_module(self, filename):
        from .extern import problog_export
        import imp

        problog_export.database = self

        module_name = os.path.splitext(os.path.split(filename)[-1])[0]
        with open(filename, "r") as extfile:
            imp.load_module(module_name, extfile, filename, (".py", "U", 1))

        return module_name, self.__extern[module_name]


class ConsultError(GroundingError):
    """Error during consult"""

    def __init__(self, message, location):
        GroundingError.__init__(self, message, location)


def _atom_to_filename(atom):
    """Translate an atom to a filename.

   :param atom: filename as atom
   :type atom: Term
   :return: filename as string
   :rtype: str
    """
    atomstr = str(atom)
    if atomstr[0] == atomstr[-1] == "'":
        atomstr = atomstr[1:-1]
    return atomstr


class PrologFunction(object):
    def __init__(self, database, functor, arity):
        self.database = database
        self.functor = functor
        self.arity = arity

    def __call__(self, *args):
        args = args[: self.arity - 1]
        query_term = Term(self.functor, *(args + (None,)))
        result = self.database.engine.query(self.database, query_term)
        if len(result) != 1:
            raise InvalidValue(
                "Function should return one result: %s returned %s"
                % (query_term, result)
            )
        return result[0][-1]


class AccessError(GroundingError):
    pass


class _AutoDict(dict):
    def __init__(self):
        dict.__init__(self)
        self.__record = set()
        self.__anon = 0
        self.__localmode = False
        self.local_variables = set()

    def enter_local(self):
        self.__localmode = True

    def exit_local(self):
        self.__localmode = False

    def __getitem__(self, key):
        if key == "_" and self.__localmode:
            key = "_#%s" % len(self.local_variables)

        if key == "_" or key is None:

            value = len(self)
            self.__anon += 1
            return value
        else:
            value = self.get(key)
            if value is None:
                value = len(self)
                self[key] = value
                if self.__localmode:
                    self.local_variables.add(value)
            elif not self.__localmode and value in self.local_variables:
                # Variable initially defined in local scope is reused outside local scope.
                # This means it's not local anymore.
                self.local_variables.remove(value)
            self.__record.add(value)
            return value

    def __len__(self):
        return dict.__len__(self) + self.__anon

    def usedVars(self):
        result = set(self.__record)
        self.__record.clear()
        return result

    def define(self, key):
        if key not in self:
            value = len(self)
            self[key] = value


def intersection(l1, l2):
    i = 0
    j = 0
    n1 = len(l1)
    n2 = len(l2)
    r = []
    a = r.append
    while i < n1 and j < n2:
        if l1[i] == l2[j]:
            a(l1[i])
            i += 1
            j += 1
        elif l1[i] < l2[j]:
            i += 1
        else:
            j += 1
    return r


class ClauseIndexNew(list):
    """ Trie version """
    def __init__(self, parent, arity):
        list.__init__(self)
        self.__parent = parent
        # self.__basetype = OrderedSet
        self.__index = TermTrieNode(functor=None, arity=None, arg_stack=None, item=None)
        self.__optimized = False
        self.__erased = set()

    def find(self, term):
        assert isinstance(term, Term)
        #TODO: Provide option to return everything if there is only query/1 or something?
        result = self.__index.find(term)
        print(f"Searching...{term}: {result}")
        if self.__erased:
            result -= self.__erased
        return result

    def append(self, item):
        #TODO: What about List?? scope1:(0.1::a ; 0.9::b :- c). yields a list [0.1::a ; 0.9::b].
        list.append(self, item)
        node = self.__parent.get_node(item)
        term = Term(node.functor, *node.args)
        print(f"Added {term} as {item}")
        self.__index.add_term(term, item)
        print("New tree")
        print(self.__index)
        print("")

    def erase(self, items):
        self.__erased |= set(items)


class TrieItemTuple(tuple):
    """
    Used by TermTrieNode. Do NOT use this for anything else.
    A tuple storing the itemID in the trie as well as the 'timestep' it was inserted at.
    Two TrieItemTuple are equal if the itemID matches.
    """

    def __new__(cls, *args):
        return tuple.__new__(cls, args)

    def __hash__(self):
        return hash(self[0])

    def __eq__(self, other):
        return self[0] == other[0]


class TermTrieNode:
    """
    #TODO: describe

    Note: The insertion order of items are taken into account when returning find results
    (uses TrieItemTuple to pack the items when inserting).

    """

    def __init__(self, functor, arity, arg_stack, item):
        """
        Use the static methods #TODO: complete

        :param functor: The functor of the first argument
        :type functor: str
        :param arity: The arity of the first argument
        :type arity: int
        :param arg_stack: Stack of remaining args. If any, then at least arity number of elements should be in here.
        :type arg_stack: List[Term]
        :param item: The item to store, this should be a TrieItemTuple or a set of those
        :param next_arg: #TODO
        """
        self.functor = str(functor) if functor is not None else functor  # Trie edge label
        self.arity = arity  # Trie edge label
        self.children = []  # Trie children of this node
        self.var_child = None  # Trie child of this node for which next arg is a Var
        self.arg_stack = arg_stack
        self.item_nb = 0
        if item is None:  # item = set of tuples (itemID, insertionOrder)
            self.item = set()
        elif isinstance(item, set):
            self.item = item
        else:
            assert isinstance(item, TrieItemTuple)
            self.item = {item} #TODO: Can this be multiple or always one??
        self.next_arg = None  #TODO: needed when expanded; used to skip this argument #TODO: Still needed?

    @staticmethod
    def create_leaf(functor, arity, arg_stack, item):
        return TermTrieNode(functor, arity, arg_stack, item)

    @staticmethod
    def _create_intermediate(functor, arity):
        return TermTrieNode(functor, arity, None, None)

    def is_leaf(self):
        """ Whether this is a leaf node, having no children """
        return self.arg_stack is not None and len(self.children) == 0 and self.var_child is None

    def is_empty(self):
        """ Whether this does not contain any element yet, so is neither a leaf nor an intermediate node """
        return self.arg_stack is None and len(self.children) == 0 and self.var_child is None

    def find(self, term):
        """
        #TODO
        :param term:
        :return:
        """
        # Obtain result. Sort based on insertionOrder (2nd elem of item_tuple)
        result = sorted(self._find(arg_stack=[term]), key=lambda item_tuple: item_tuple[1])
        # Return results in that order but omit itemOrder
        return [itemID for itemID, itemOrder in result]

    def _find(self, arg_stack, arg_skip_count=0):
        """ Auxiliary method of find/1
        :param arg_stack:
        :param arg_skip_count: The number of next arguments to skip
        :return:
        """
        if self.is_leaf():
            c_arg_stack = self.arg_stack.copy()
            # skip args
            while arg_skip_count > 0:
                arg_skip_count -= 1
                c_arg_stack.pop()
            # check match and if so, return self.item
            if len(arg_stack) != len(c_arg_stack):
                return set()
            if len(arg_stack) == 0:
                return self.item
            c_next_arg = c_arg_stack.pop()
            next_arg = arg_stack.pop()
            #TODO: Does type have to be same?
            while (len(c_arg_stack) > 0 and len(arg_stack) > 0) and \
                    (isinstance(c_next_arg, Var) or isinstance(next_arg, Var) or
                     (c_next_arg.arity == next_arg.arity and c_next_arg.functor == next_arg.functor)):
                if not(isinstance(c_next_arg, Var) or isinstance(next_arg, Var)):
                    # If it were vars, we would have skipped this arg
                    self._add_args_to_stack(c_next_arg, c_arg_stack)
                    self._add_args_to_stack(next_arg, arg_stack)
                c_next_arg = c_arg_stack.pop()
                next_arg = arg_stack.pop()
            if len(c_arg_stack) == 0 and len(arg_stack) == 0:
                return self.item
            else:
                assert len(c_arg_stack) != 0 and len(arg_stack) != 0
                return set()
        else:
            results = set()
            if arg_skip_count > 0:
                # We are still skipping args, not checking yet
                arg_skip_count -= 1  # skipped next functor
                for node in self.children:
                    #TODO: Can we avoid .copy() if we don't yet use it anyway?
                    results |= node._find(arg_stack.copy(), arg_skip_count=arg_skip_count + node.arity)
                if self.var_child is not None:
                    results |= self.var_child._find(arg_stack.copy(), arg_skip_count=arg_skip_count + self.var_child.arity)
            else:
                first_arg = arg_stack.pop()
                if self.var_child is not None:
                    # Add any items from Variable child
                    results |= self.var_child._find(arg_stack.copy())
                if isinstance(first_arg, Var):
                    # Go over all children but jump appropriately to the next arg..
                    for node in self.children:
                        results |= node._find(arg_stack.copy(), arg_skip_count=node.arity)
                else:
                    # Find matching child
                    functor = first_arg.functor
                    arity = first_arg.arity
                    insertion_index = self._term_binary_search(self.children, str(functor), arity)
                    if len(self.children) == insertion_index:
                        matching_node = None
                        found_match = False
                    else:
                        matching_node = self.children[insertion_index]
                        found_match = matching_node.arity == arity and matching_node.functor == str(functor)
                    if found_match:
                        # Continue search within matching child
                        self._add_args_to_stack(first_arg, arg_stack)
                        results |= matching_node._find(arg_stack)
            return results


    def _add_args_to_stack(self, term, arg_stack):
        new_args = [a if not type(a) == int else Var(a) for a in term.args].__reversed__()
        arg_stack.extend(new_args)

    def _expand_to_intermediate(self, arg_stack, item):
        if len(arg_stack) == 0:
            # If arg_stack empty than both terms match exactly
            # Just update items, do not turn into intermediate node
            # case example: 0.1::a(1). 0.2::a(2). 0.3::a(1).
            assert(len(self.arg_stack) == 0)  # Otherwise there would have been an arity difference
            self.item.add(item)
            #self.item.add(item)
            return

        #TODO: (opt) If they're the same then don't unroll the entire thing (just undo somehow?, copy the arg_stack)
        arg_stack_backup = self.arg_stack.copy()
        # Compare the next tuple of functor, arity
        new_next_arg = arg_stack.pop()
        self._add_args_to_stack(new_next_arg, arg_stack)
        curr_next_arg = self.arg_stack.pop()
        self._add_args_to_stack(curr_next_arg, self.arg_stack)
        diff_node = self  # The node on which both arg_stacks differ
        while len(arg_stack) > 0 and len(self.arg_stack) > 0 and \
                type(new_next_arg) == type(curr_next_arg) and \
                ((new_next_arg.functor == curr_next_arg.functor and new_next_arg.arity == curr_next_arg.arity) or \
                (isinstance(new_next_arg, Var) and isinstance(curr_next_arg, Var))):
            # Keep unrolling until stacks are different (next_args are different)
            new_diff_node = TermTrieNode._create_intermediate(curr_next_arg.functor, curr_next_arg.arity)
            if isinstance(curr_next_arg, Var):
                diff_node.var_child = new_diff_node
            else:
                diff_node.children.append(new_diff_node)
            diff_node = new_diff_node
            # Unroll next
            new_next_arg = arg_stack.pop()
            self._add_args_to_stack(new_next_arg, arg_stack)
            curr_next_arg = self.arg_stack.pop()
            self._add_args_to_stack(curr_next_arg, self.arg_stack)

        equivalent_stacks = (len(arg_stack) == 0 == len(self.arg_stack)) and \
                            new_next_arg.functor == curr_next_arg.functor and \
                            new_next_arg.arity == curr_next_arg.arity
        if equivalent_stacks or (isinstance(new_next_arg, Var) and isinstance(curr_next_arg, Var)):
            # They were equivalent. Restore stack and just add the item
            self.arg_stack = arg_stack_backup
            #self.item.add(item)
            self.item.add(item)
            self.children = []
            self.var_child = None
        else:
            # Create two different children in last unrolled
            child1 = TermTrieNode.create_leaf(curr_next_arg.functor, curr_next_arg.arity, self.arg_stack, self.item)
            child2 = TermTrieNode.create_leaf(new_next_arg.functor, new_next_arg.arity, arg_stack, item)
            if isinstance(curr_next_arg, Var):
                diff_node.var_child = child1
                diff_node.children.append(child2)  # They can't both be Var (see while loop)
            elif isinstance(new_next_arg, Var):
                diff_node.var_child = child2
                diff_node.children.append(child1)  # They can't both be Var (see while loop)
            else:
                # Add them both in a sorted manner
                diff_node.children = [child1]
                insertion_index = self._term_binary_search(diff_node.children, str(child2.functor), child2.arity)
                diff_node.children.insert(insertion_index, child2)

            # Convert current into intermediate node
            self.arg_stack = None
            self.item = {}

    def _add_term_args(self, arg_stack: list, item):
        """ Auxiliary method of add_term/2 """
        if self.is_empty():
            self.arg_stack = arg_stack
            self.item = {item}
        elif self.is_leaf():
            # If not the same, expand till they're different
            self._expand_to_intermediate(arg_stack, item)
        else:
            first_arg = arg_stack.pop()
            functor = first_arg.functor
            arity = first_arg.arity
            self._add_args_to_stack(first_arg, arg_stack)

            if isinstance(first_arg, Var):
                # If first_arg is variable, store item under var_child
                if self.var_child is not None:
                    # Expand existing Var entry
                    self.var_child._add_term_args(arg_stack, item)
                else:
                    self.var_child = TermTrieNode.create_leaf(functor, arity, arg_stack, item)
            else:
                # find child with matching <functor,arity> for first arg.
                insertion_index = self._term_binary_search(self.children, str(functor), arity)
                if len(self.children) == 0 or insertion_index == len(self.children):
                    # No match
                    matching_node = None
                    found_match = False
                else:
                    # Maybe match
                    matching_node = self.children[insertion_index]
                    found_match = matching_node.arity == arity and matching_node.functor == str(functor)

                if found_match:
                    # If match, expand in matching node
                    matching_node._add_term_args(arg_stack, item)
                else:
                    # If no match, create new child node
                    new_child = TermTrieNode.create_leaf(first_arg.functor, first_arg.arity, arg_stack, item)
                    self.children.insert(insertion_index, new_child)

    def add_term(self, term, item):
        """
        # TODO
        :param term:
        :param item:
        :return:
        """
        self.item_nb += 1
        return self._add_term_args([term], TrieItemTuple(item, self.item_nb))

    @staticmethod
    def _term_binary_search(child_list, functor, arity):
        """
        Performs a binary search for term on the list of children.
        Assumes term is not an instance of Var
        Code based on bisect_left of python3.6/bisect.py
        :param child_list: The list of trie nodes to search in. Will use functor and arity to compare.
        :type child_list: List[TermTrieNode]
        :param functor: The functor of (functor,arity) which is searched for.
        :type functor: str
        :param arity: The arity of (functor,arity) which is searched for.
        :type arity: int
        :return: The index where to find (functor,arity) if it was present.
        """
        # assert(not isinstance(term, Var))
        lo = 0
        hi = len(child_list)

        while lo < hi:
            mid = (lo + hi) // 2
            if child_list[mid].arity < arity or \
                    child_list[mid].arity == arity and child_list[mid].functor < functor:
                lo = mid + 1
            else:
                hi = mid
        return lo

    def _str_aux(self, level=0):
        """ Auxiliary function for __str__ to pass on indentation level """
        if self.is_leaf():
            return f"{self.functor}/{self.arity}: item={self.item}, stack={self.arg_stack}"
        else:
            intend = f"\t" * level
            result = f"{self.functor}/{self.arity}"
            if self.var_child is not None:
                result += f"\n{intend}- Var: {self.var_child._str_aux(level+1)}"
            for child in self.children:
                result += f"\n{intend}- {child._str_aux(level+1)}"
            return result

    def __str__(self, level=0):
        return self._str_aux()

    # self.item_nb does not match length if an item was rejected due to being a duplicate
    # def __len__(self):
    #     return self.item_nb


class ClauseIndexOld(list):
    def __init__(self, parent, arity):
        list.__init__(self)
        self.__parent = parent
        self.__basetype = OrderedSet
        self.__index = [defaultdict(self.__basetype) for _ in range(0, arity)]
        self.__optimized = False
        self.__erased = set()

    def find(self, term):
        arguments = term.args
        results = None
        for i, arg in enumerate(arguments):
            if not is_ground(arg):
                pass  # Variable => no restrictions
            else:
                curr = self.__index[i].get(arg, self.__basetype())
                curr |= self.__index[i].get(None, self.__basetype())

                if results is None:  # First argument with restriction
                    results = curr
                else:
                    results = results & curr  # for some reason &= doesn't work here
            if results is not None and not results:
                print(f"Searching...{term}: {set(results)}")
                return []
        if results is None:
            if self.__erased:
                print(f"Searching...{term}: {OrderedSet(self) - self.__erased}")
                return OrderedSet(self) - self.__erased
            else:
                print(f"Searching...{term}: {set(self)}")
                return self
        else:
            if self.__erased:
                print(f"Searching...{term}: {results - self.__erased}")
                return results - self.__erased
            else:
                print(f"Searching...{term}: {set(results)}")
                return results

    def _add(self, key, item):
        for i, k in enumerate(key):
            self.__index[i][k].add(item)

    def append(self, item):
        list.append(self, item)
        node = self.__parent.get_node(item)
        try:
            args = node.args
        except AttributeError:
            args = [None] * self.__parent.get_node(item).arity
        key = [arg if is_ground(arg) else None for arg in args]
        term = Term(node.functor, *args)
        print(f"Added {term} as {item}")
        self._add(key, item)

    def erase(self, items):
        self.__erased |= set(items)

ClauseIndex = ClauseIndexNew