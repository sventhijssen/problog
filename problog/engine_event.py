"""
Part of the ProbLog distribution.

Copyright 2015 KU Leuven, DTAI Research Group

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
"""
from __future__ import print_function
from collections import defaultdict

import sys, os
import imp, inspect # For load_external

from .formula import LogicFormula
from .program import PrologFile
from .logic import Term
from .engine import unify, UnifyError, instantiate, extract_vars, is_ground, UnknownClause, UnknownClauseInternal, ConsultError
from .engine import addStandardBuiltIns, check_mode, GroundingError, NonGroundProbabilisticClause
from .engine import ClauseDBEngine, ClauseDB


class Context(object) :
    """Variable context."""

    def __init__(self, lst, define) :
        self.__lst = lst
        self.define = define

    def __getitem__(self, index) :
        return self.__lst[index]

    def __setitem__(self, index, value) :
        self.__lst[index] = value

    def __len__(self) :
        return len(self.__lst)

    def __iter__(self) :
        return iter(self.__lst)

    def __str__(self) :
        return str(self.__lst)


class EventBasedEngine(ClauseDBEngine) :
    """An event-based ProbLog grounding engine. It supports cyclic programs."""

    def __init__(self, *args, **kwdargs) :
        ClauseDBEngine.__init__(self,*args, **kwdargs)

    def _create_context(self, lst=[], size=None, define=None) :
        """Create a new context."""
        if size != None :
            assert( not lst )
            lst = [None] * size
        return Context(lst, define=define)

    def loadBuiltIns(self) :
        addBuiltIns(self)

    def execute(self, node_id, database=None, context=None, target=None, location=None, allow_vars=True, **kwdargs ) :
        # Create a new call.
        call_node = ClauseDB._call( '???', context, node_id, location )
        # Initialize a result collector callback.
        res = ResultCollector(allow_vars, database=database, location=location)
        try :
            # Evaluate call.
            self._eval(database, target, node_id, self._create_context(context,define=None), res )
        except RuntimeError as err :
            if str(err).startswith('maximum recursion depth exceeded') :
                raise CallStackError()
            else :
                raise
        # Return ground program and results.
        return [ (x,y) for y,x in res.results ]

    def _eval(self, db, gp, node_id, context, parent) :
        # Find the node and determine its type.
        node = db.getNode( node_id )
        ntype = type(node).__name__
        # Notify debugger of enter event.
        # Select appropriate method for handling this node type.
        if node == () :
            raise UnknownClauseInternal()
        elif ntype == 'fact' :
            f = self._eval_fact
        elif ntype == 'choice' :
            f = self._eval_choice
        elif ntype == 'define' :
            f = self._eval_define
        elif ntype == 'clause' :
            f = self._eval_clause
        elif ntype == 'conj' :
            f = self._eval_conj
        elif ntype == 'disj' :
            f = self._eval_disj
        elif ntype == 'call' :
            f = self._eval_call
        elif ntype == 'neg' :
            f = self._eval_neg
        else :
            raise ValueError(ntype)
        # Evaluate the node.
        f(db, gp, node_id, node, context, parent)
        # Notify debugger of exit event.

    def _eval_fact( self, db, gp, node_id, node, call_args, parent ) :
        try :
            # Verify that fact arguments unify with call arguments.
            for a,b in zip(node.args, call_args) :
                unify(a, b)
            # Successful unification: notify parent callback.
            parent.newResult( node.args, ground_node=gp.add_atom(node_id, node.probability) )
        except UnifyError :
            # Failed unification: don't send result.
            pass
        # Send complete message.
        parent.complete()

    def _eval_choice( self, db, gp, node_id, node, call_args, parent ) :
        # This never fails.
        # Choice is ground so result is the same as call arguments.
        result = tuple(call_args)

        # Raise error when head is not ground.
        if not is_ground(*result) : raise NonGroundProbabilisticClause(location=db.lineno(node.location))

        # Ground probability.
        probability = instantiate( node.probability, call_args )
        # Create a new atom in ground program.
        origin = (node.group, result)
        ground_node = gp.add_atom( (node.group, result, node.choice) , probability, group=(node.group, result) )
        # Notify parent.
        parent.newResult( result, ground_node )
        parent.complete()

    def _eval_call( self, db, gp, node_id, node, context, parent ) :
        # Ground the call arguments based on the current context.
        call_args = [ instantiate(arg, context) for arg in node.args ]
        # Create a context switching node that unifies the results of the call with the call arguments. Results are passed to the parent callback.
        context_switch = ProcessCallReturn( node.args, context, parent )
        # Evaluate the define node.
        if node.defnode < 0 :
            # Negative node indicates a builtin.
            builtin = self._get_builtin( node.defnode )
            builtin( *call_args, context=context, callback=context_switch, database=db, engine=self, ground_program=gp, location=node.location )
        else :
            # Positive node indicates a non-builtin.
            try :
                # Evaluate the define node.
                self._eval( db, gp, node.defnode, self._create_context(call_args, define=context.define), context_switch )
            except UnknownClauseInternal :
                # The given define node is empty: no definition found for this clause.
                sig = '%s/%s' % (node.functor, len(node.args))
                raise UnknownClause(sig, location=db.lineno(node.location))

    def _eval_clause( self, db, gp, node_id, node, call_args, parent ) :
        try :
            # Create a new context (i.e. variable values).
            context = self._create_context(size=node.varcount,define=call_args.define)
            # Fill in the context by unifying clause head arguments with call arguments.
            for head_arg, call_arg in zip(node.args, call_args) :
                # Remove variable identifiers from calling context.
                if type(call_arg) == int : call_arg = None
                # Unify argument and update context (raises UnifyError if not possible)
                unify( call_arg, head_arg, context)
            # Create a context switching node that extracts the head arguments from the results obtained by evaluating the body. These results are send by the parent.
            context_switch = ProcessBodyReturn( node.args, node, node_id, parent )
            # Evaluate the body. Use context-switch as callback.
            self._eval( db, gp, node.child, context, context_switch )
        except UnifyError :
            # Call and clause head are not unifiable, just fail (complete without results).
            parent.complete()

    def _eval_conj( self, db, gp, node_id, node, context, parent ) :
        # Extract children (always exactly two).
        child1, child2 = node.children
        # Create a link between child1 and child2.
        # The link receives results of the first child and evaluates the second child based on the result.
        # The link receives the complete event from the first child and passes it to the parent.
        process = ProcessLink( self, db, gp, child2, parent, context.define )
        # Start evaluation of first child.
        self._eval( db, gp, child1, context, process )

    def _eval_disj( self, db, gp, node_id, node, context, parent ) :
        # Create a disjunction processor node, and register parent as listener.
        process = ProcessOr( len(node.children), parent )
        # Process all children.
        for child in node.children :
            self._eval( db, gp, child, context, process )

    def _eval_neg(self, db, gp, node_id, node, context, parent) :
        # Create a negation processing node, and register parent as listener.
        process = ProcessNot( gp, context, parent)
        # Evaluate the child node. Use processor as callback.
        self._eval( db, gp, node.child, context, process )

    def _eval_define( self, db, gp, node_id, node, call_args, parent ) :
        # Create lookup key. We will reuse results for identical calls.
        # EXTEND support call subsumption?
        key = (node_id, tuple(call_args))

        # Store cache in ground program
        if not hasattr(gp, '_def_nodes') : gp._def_nodes = {}
        def_nodes = gp._def_nodes

        # Find pre-existing node.
        pnode = def_nodes.get(key)
        if pnode is None :
            # Node does not exist: create it and add it to the list.
            pnode = ProcessDefine( self, db, gp, node_id, node, call_args, call_args.define )
            def_nodes[key] = pnode
            # Add parent as listener.
            pnode.addListener(parent)
            # Execute node. Note that for a given call (key), this is only done once!
            pnode.execute()
        else :
            # Node exists already.
            if call_args.define and call_args.define.hasAncestor(pnode) :
                # Cycle detected!
                # EXTEND Mark this information in the ground program?
                cnode = ProcessDefineCycle(pnode, call_args.define, parent)
            else :
                # Add ancestor here.
                pnode.addAncestor(call_args.define)
                # Not a cycle, just reusing. Register parent as listener (will retrigger past events.)
                pnode.addListener(parent)

    def add_external_calls(self, externals):
        self.__externals = externals

    def get_external_call(self, func_name):
        if self.__externals is None or not func_name in self.__externals:
            return None
        return self.__externals[func_name]


class ProcessNode(object) :
    """Generic class for representing *process nodes*."""

    EVT_COMPLETE = 1
    EVT_RESULT = 2
    EVT_ALL = 3

    def __init__(self) :
        EngineLogger.get().create(self)
        self.listeners = []
        self.isComplete = False

    def notifyListeners(self, result, ground_node=0) :
        """Send the ``newResult`` event to all the listeners of this node.
            The arguments are used as the arguments of the event.
        """
        EngineLogger.get().sendResult(self, result, ground_node)
        for listener, evttype in self.listeners :
            if evttype & self.EVT_RESULT :
                listener.newResult(result, ground_node)

    def notifyComplete(self) :
        """Send the ``complete`` event to all listeners of this node."""

        EngineLogger.get().sendComplete(self)
        if not self.isComplete :
            self.isComplete = True
            for listener, evttype in self.listeners :
                if evttype & self.EVT_COMPLETE :
                    listener.complete()

    def addListener(self, listener, eventtype=EVT_ALL) :
        """Add the given listener."""
        # Add the listener such that it receives future events.
        EngineLogger.get().connect(self,listener,eventtype)
        self.listeners.append((listener,eventtype))

    def complete(self) :
        """Process a ``complete`` event.

        By default forwards this events to its listeners.
        """
        EngineLogger.get().receiveComplete(self)
        self.notifyComplete()

    def newResult(self, result, ground_node=0) :
        """Process a new result.

        :param result: context or list of arguments
        :param ground_node: node is ground program

        By default forwards this events to its listeners.
        """
        EngineLogger.get().receiveResult(self, result, ground_node)
        self.notifyListeners(result, ground_node)

class ProcessOr(ProcessNode) :
    """Process a disjunction of nodes.

    :param count: number of disjuncts
    :type count: int
    :param parent: listener
    :type parent: ProcessNode

    Behaviour:

        * This node forwards all results to its listeners.
        * This node sends a complete message after receiving ``count`` ``complete`` messages from its children.
        * If the node is initialized with ``count`` equal to 0, it sends out a ``complete`` signal immediately.

    """

    def __init__(self, count, parent) :
        ProcessNode.__init__(self)
        self._count = count
        self.addListener(parent)
        if self._count == 0 :
            self.notifyComplete()

    def complete(self) :
        EngineLogger.get().receiveComplete(self)
        self._count -= 1
        if self._count <= 0 :
            self.notifyComplete()

class ProcessNot(ProcessNode) :
    """Process a negation node.

    Behaviour:

        * This node buffers all ground nodes.
        * Upon receiving a ``complete`` event, sends a result and a ``complete`` signal.
        * The result is negation of an ``or`` node of its children. No result is send if the children are deterministically true.

    """

    def __init__(self, gp, context, parent) :
        ProcessNode.__init__(self)
        self.context = context
        self.ground_nodes = []
        self.gp = gp
        self.addListener(parent)

    def newResult(self, result, ground_node=0) :
        EngineLogger.get().receiveResult(self, result, ground_node)
        if ground_node != None :
            self.ground_nodes.append(ground_node)

    def complete(self) :
        EngineLogger.get().receiveComplete(self)
        if self.ground_nodes :
            or_node = self.gp.add_not(self.gp.add_or( self.ground_nodes ))
            if or_node != None :
                self.notifyListeners(self.context, ground_node=or_node)
        else :
            self.notifyListeners(self.context, ground_node=0)
        self.notifyComplete()

class ProcessLink(ProcessNode) :
    """Links two calls in a conjunction."""


    def __init__(self, engine, db, gp, node_id, parent, define) :
        ProcessNode.__init__(self)
        self.engine = engine
        self.db = db
        self.gp = gp
        self.node_id = node_id
        self.parent = parent
        self.addListener(self.parent, ProcessNode.EVT_COMPLETE)
        self.define = define
        self.required_complete = 1

    def newResult(self, result, ground_node=0) :
        self.required_complete += 1     # For each result of first conjuct, we call the second conjuct which should produce a complete.
        EngineLogger.get().receiveResult(self, result, ground_node, 'required: %s' % self.required_complete)
        process = ProcessAnd(self.gp, ground_node)
        process.addListener(self.parent, ProcessNode.EVT_RESULT)
        process.addListener(self, ProcessNode.EVT_COMPLETE) # Register self as listener for complete events.
        self.engine._eval( self.db, self.gp, self.node_id, self.engine._create_context(result,define=self.define), process)

    def complete(self) :
        # Receive complete
        EngineLogger.get().receiveComplete(self, 'required: %s' % (self.required_complete-1))
        self.required_complete -= 1
        if self.required_complete == 0 :
            self.notifyComplete()


class ProcessAnd(ProcessNode) :
    """Process a conjunction."""

    def __init__(self, gp, first_node ) :
        ProcessNode.__init__(self)
        self.gp = gp
        self.first_node = first_node

    def newResult(self, result, ground_node=0) :
        EngineLogger.get().receiveResult(self, result, ground_node)
        and_node = self.gp.add_and( (self.first_node, ground_node) )
        self.notifyListeners(result, and_node)

class ProcessDefineCycle(ProcessNode) :
    """Process a cyclic define (child)."""

    def __init__(self, parent, context, listener) :
        self.parent = parent

        context.propagateCyclic(self.parent)

        # while context != self.parent :
        #     context.cyclic = True
        #     context = context.parent
        ProcessNode.__init__(self)
        self.addListener(listener)
        self.parent.addListener(self)
        self.parent.cyclic = True
        self.parent.addCycleChild(self)


    def __repr__(self) :
        return 'cycle child of %s [%s]' % (self.parent, id(self))


class ProcessDefine(ProcessNode) :
    """Process a standard define (or cycle parent)."""

    def __init__(self, engine, db, gp, node_id, node, args, parent) :
        self.node = node
        self.results = {}
        self.engine = engine
        self.db = db
        self.gp = gp
        self.node_id = node_id
        self.args = args
        self.parents = set()
        if parent : self.parents.add(parent)
        self.__is_cyclic = False
        self.__buffer = defaultdict(list)
        self.children = []
        self.execute_completed = False
        ProcessNode.__init__(self)

    @property
    def cyclic(self) :
        return self.__is_cyclic

    @cyclic.setter
    def cyclic(self, value) :
        if self.__is_cyclic != value :
            self.__is_cyclic = value
            self._cycle_detected()

    def propagateCyclic(self, root) :
        if root != self :
            self.cyclic = True
            for p in self.parents :
                p.propagateCyclic(root)

    def addCycleChild(self, cnode ) :
        self.children.append(cnode)
        if self.execute_completed :
            cnode.complete()

    def addAncestor(self, parent) :
        self.parents.add(parent)

    def getAncestors(self) :
        current = {self}
        just_added = current
        while just_added :
            latest = set()
            for a in just_added :
                latest |= a.parents
            latest -= current
            current |= latest
            just_added = latest
        return current

    def hasAncestor(self, anc) :
        return anc in self.getAncestors()

    def addListener(self, listener, eventtype=ProcessNode.EVT_ALL) :

        # Add the listener such that it receives future events.
        ProcessNode.addListener(self, listener, eventtype)

        # If the node was already active, notify listener of past events.
        for result, ground_node in list(self.results.items()) :
            if eventtype & ProcessNode.EVT_RESULT :
                listener.newResult(result, ground_node)

        if self.isComplete :
            if eventtype & ProcessNode.EVT_COMPLETE :
                listener.complete()

    def execute(self) :
        # Get the appropriate children
        children = self.node.children.find( self.args )

        process = ProcessOr( len(children), self)
        # Evaluate the children
        for child in children :
            self.engine._eval( self.db, self.gp, child, self.engine._create_context(self.args,define=self), parent=process )

        self.execute_completed = True
        for c in self.children :
            c.complete()


    def newResult(self, result, ground_node=0) :
        EngineLogger.get().receiveResult(self, result, ground_node)
        if self.cyclic :
            self.newResultUnbuffered(result, ground_node)
        else :
            self.newResultBuffered(result, ground_node)

    def newResultBuffered(self, result, ground_node=0) :
        res = (tuple(result))
        self.__buffer[res].append( ground_node )

    def newResultUnbuffered(self, result, ground_node=0) :
        res = (tuple(result))
        if res in self.results :
            res_node = self.results[res]
            self.gp.add_disjunct( res_node, ground_node )
        else :
            result_node = self.gp.add_or( (ground_node,), readonly=False )
            self.results[ res ] = result_node

            self.notifyListeners(result, result_node )

    def complete(self) :
        EngineLogger.get().receiveComplete(self)
        self._flush_buffer()
        self.notifyComplete()

    def _cycle_detected(self) :
        self._flush_buffer(True)

    def _flush_buffer(self, cycle=False) :
        for result, nodes in self.__buffer.items() :
            if len(nodes) > 1 or cycle :
                # Must make an 'or' node
                node = self.gp.add_or( nodes, readonly=(not cycle) )
            else :
                node = nodes[0]
            self.results[result] = node
            self.notifyListeners(result, node)
        self.__buffer.clear()


    def __repr__(self) :
        return '%s %s(%s)' % (id(self), self.node.functor, ', '.join(map(str,self.args)))

class ProcessBodyReturn(ProcessNode) :
    """Process the results of a clause body."""

    def __init__(self, head_args, node, node_id, parent) :
        ProcessNode.__init__(self)
        self.head_args = head_args
        self.head_vars = extract_vars(*self.head_args)
        self.node_id = node_id
        self.node = node
        self.addListener(parent)

    def newResult(self, result, ground_node=0) :
        for i, res in enumerate(result) :
            if not is_ground(res) and self.head_vars[i] > 1 :
                raise VariableUnification(location=self.node.location)

        EngineLogger.get().receiveResult(self, result, ground_node)
        output = [ instantiate(arg, result) for arg in self.head_args ]
        self.notifyListeners(output, ground_node)

class ProcessCallReturn(ProcessNode) :
    """Process the results of a call."""

    def __init__(self, call_args, context, parent) :
        ProcessNode.__init__(self)
        self.call_args = call_args
        self.context = context
        self.addListener(parent)

    def newResult(self, result, ground_node=0) :
        EngineLogger.get().receiveResult(self, result, ground_node)
        output = list(self.context)
        try :
            for call_arg, res_arg in zip(self.call_args,result) :
                unify( res_arg, call_arg, output )
            self.notifyListeners(output, ground_node)
        except UnifyError :
            pass


class ResultCollector(ProcessNode) :
    """Collect results."""

    def __init__(self, allow_vars=True, database=None, location=None) :
        ProcessNode.__init__(self)
        self.results = []
        self.allow_vars = allow_vars
        self.location = location
        self.database = database

    def newResult( self, result, ground_result) :
        if not self.allow_vars and not is_ground(*result) :
            if self.database :
                location = self.database.lineno(self.location)
            else :
                location = None
            raise NonGroundProbabilisticClause(location=location)

        self.results.append( (ground_result, result  ))

    def complete(self) :
        pass

class PrologInstantiationError(Exception) : pass

class PrologTypeError(Exception) : pass







class CallProcessNode(object) :

    def __init__(self, term, args, parent) :
        self.term = term
        self.num_args = len(args)
        self.parent = parent

    def newResult(self, result, ground_node=0) :
        if self.num_args > 0 :
            res1 = result[:-self.num_args]
            res2 = result[-self.num_args:]
        else :
            res1 = result
            res2 = []
        self.parent.newResult( [self.term(*res1)] + list(res2), ground_node )

    def complete(self) :
        self.parent.complete()


def builtin_call( term, args=(), callback=None, database=None, engine=None, context=None, ground_program=None, **kwdargs ) :
    check_mode( (term,), 'c', functor='call' )
    # Find the define node for the given query term.
    clause_node = database.find(term.withArgs( *(term.args+args)))
    # If term not defined: try loading it as a builtin
    if clause_node is None : clause_node = database._get_builtin(term.signature)
    # If term not defined: raise error
    if clause_node is None : raise UnknownClause(term.signature, location=database.lineno(term.location))
    # Create a new call.
    call_node = ClauseDB._call( term.functor, range(0, len(term.args) + len(args)), clause_node, None )
    # Create a callback node that wraps the results in the functor.
    cb = CallProcessNode(term, args, callback)
    # Evaluate call.
    engine._eval_call(database, ground_program, None, call_node, engine._create_context(term.args+args,define=context.define), cb )

def builtin_callN( term, *args, **kwdargs ) :
    return builtin_call(term, args, **kwdargs)


class BooleanBuiltIn(object) :
    """Simple builtin that consist of a check without unification. (e.g. var(X), integer(X), ... )."""

    def __init__(self, base_function) :
        self.base_function = base_function

    def __call__( self, *args, **kwdargs ) :
        callback = kwdargs.get('callback')
        if self.base_function(*args, **kwdargs) :
            callback.newResult(args)
        callback.complete()

class SimpleBuiltIn(object) :
    """Simple builtin that does cannot be involved in a cycle or require engine information and has 0 or more results."""

    def __init__(self, base_function) :
        self.base_function = base_function

    def __call__(self, *args, **kwdargs ) :
        callback = kwdargs.get('callback')
        results = self.base_function(*args, **kwdargs)
        if results :
            for result in results :
                callback.newResult(result)
        callback.complete()



def addBuiltIns(engine) :

    addStandardBuiltIns(engine, BooleanBuiltIn, SimpleBuiltIn )

    # These are special builtins
    engine.add_builtin('call', 1, builtin_call)
    for i in range(2,10) :
        engine.add_builtin('call', i, builtin_callN)



DefaultEngine = EventBasedEngine


class UserAbort(Exception) : pass

class UserFail(Exception) : pass


class CallStackError(GroundingError) :

    def __init__(self) :
        GroundingError.__init__(self, 'The grounding engine exceeded the maximal recursion depth.')


# Input python 2 and 3 compatible input
try:
    input = raw_input
except NameError:
    pass

class Debugger(object) :

    def __init__(self, debug=True, trace=False) :
        self.__debug = debug
        self.__trace = trace
        self.__trace_level = None

    def enter(self, level, node_id, call_args) :
        if self.__trace :
            print ('  ' * level, '>', node_id, call_args, end='')
            self._trace(level)
        elif self.__debug :
            print ('  ' * level, '>', node_id, call_args)

    def exit(self, level, node_id, call_args, result) :

        if not self.__trace and level == self.__trace_level :
            self.__trace = True
            self.__trace_level = None

        if self.__trace :
            if result == 'USER' :
                print ('  ' * level, '<', node_id, call_args, result)
            else :
                print ('  ' * level, '<', node_id, call_args, result, end='')
                self._trace(level, False)
        elif self.__debug :
            print ('  ' * level, '<', node_id, call_args, result)

    def _trace(self, level, call=True) :
        try :
            cmd = input('? ')
            if cmd == '' or cmd == 'c' :
                pass    # Do nothing special
            elif cmd.lower() == 's' :
                if call :
                    self.__trace = False
                    if cmd == 's' : self.__debug = False
                    self.__trace_level = level
            elif cmd.lower() == 'u' :
                self.__trace = False
                if cmd == 'u' : self.__debug = False
                self.__trace_level = level - 1
            elif cmd.lower() == 'l' :
                self.__trace = False
                if cmd == 'l' : self.__debug = False
                self.__trace_level = None
            elif cmd.lower() == 'a' :
                raise UserAbort()
            elif cmd.lower() == 'f' :
                if call :
                    raise UserFail()
            else : # help
                prefix = '  ' * (level) + '    '
                print (prefix, 'Available commands:')
                print (prefix, '\tc\tcreep' )
                print (prefix, '\ts\tskip     \tS\tskip (with debug)' )
                print (prefix, '\tu\tgo up    \tU\tgo up (with debug)' )
                print (prefix, '\tl\tleap     \tL\tleap (with debug)' )
                print (prefix, '\ta\tabort' )
                print (prefix, '\tf\tfail')
                print (prefix, end='')
                self._trace(level,call)
        except EOFError :
            raise UserAbort()

class EngineLogger(object) :
    """Logger for engine messaging."""

    instance = None
    instance_class = None

    @classmethod
    def get(self) :
        if EngineLogger.instance is None :
            if EngineLogger.instance_class is None :
                EngineLogger.instance = EngineLogger()
            else :
                EngineLogger.instance = EngineLogger.instance_class()
        return EngineLogger.instance

    @classmethod
    def setClass(cls, instance_class) :
        EngineLogger.instance_class = instance_class
        EngineLogger.instance = None

    def __init__(self) :
        pass

    def receiveResult(self, source, result, node, *extra) :
        pass

    def receiveComplete(self, source, *extra) :
        pass

    def sendResult(self, source, result, node, *extra) :
        pass

    def sendComplete(self, source, *extra) :
        pass

    def create(self, node) :
        pass

    def connect(self, source, listener, evt_type) :
        pass

class SimpleEngineLogger(EngineLogger) :

    def __init__(self) :
        pass

    def receiveResult(self, source, result, node, *extra) :
        print (type(source).__name__, id(source), 'receive', result, node, source, *extra)

    def receiveComplete(self, source, *extra) :
        print (type(source).__name__, id(source), 'receive complete', source, *extra)

    def sendResult(self, source, result, node, *extra) :
        print (type(source).__name__, id(source), 'send', result, node, source, *extra)

    def sendComplete(self, source, *extra) :
        print (type(source).__name__, id(source), 'send complete', source, *extra)

    def create(self, source) :
        print (type(source).__name__, id(source), 'create', source)

    def connect(self, source, listener, evt_type) :
        print (type(source).__name__, id(source), 'connect', type(listener).__name__, id(listener))
