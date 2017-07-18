from sympy import cos, sin

from devito.dse.backends import AbstractRewriter, dse_pass
from devito.dse.extended_sympy import bhaskara_cos, bhaskara_sin
from devito.dse.manipulation import common_subexprs_elimination

from devito.interfaces import ScalarFunction


class BasicRewriter(AbstractRewriter):

    def _pipeline(self, state):
        self._eliminate_intra_stencil_redundancies(state)

    @dse_pass
    def _eliminate_intra_stencil_redundancies(self, cluster, **kwargs):
        """
        Perform common subexpression elimination, bypassing the scalar expressions
        extracted in previous passes.
        """

        skip = [e for e in cluster.exprs if e.lhs.base.function.is_SymbolicFunction]
        candidates = [e for e in cluster.exprs if e not in skip]

        template = self.conventions['temporary'] + "%d"
        make = lambda i: ScalarFunction(name=template % i).indexify()

        processed = common_subexprs_elimination(candidates, make)

        return cluster.reschedule(skip + processed)

    @dse_pass
    def _optimize_trigonometry(self, cluster, **kwargs):
        """
        Rebuild ``exprs`` replacing trigonometric functions with Bhaskara
        polynomials.
        """

        processed = []
        for expr in cluster.exprs:
            handle = expr.replace(sin, bhaskara_sin)
            handle = handle.replace(cos, bhaskara_cos)
            processed.append(handle)

        return cluster.rebuild(processed)
