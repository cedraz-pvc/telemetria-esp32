"""
Tabela Hash com encadeamento separado (separate chaining).

Complexidade (caso médio, com boa função de hash e fator de carga <= 0.75):
    inserir  -> O(1)
    buscar   -> O(1)
    remover  -> O(1)
    contem   -> O(1)

Quando o fator de carga (itens / baldes) passa de 0.75 a tabela dobra de
tamanho e redistribui as chaves (rehash). O custo do rehash é O(n), mas
acontece raramente, então o custo AMORTIZADO de inserir continua O(1).

Pior caso (todas as chaves no mesmo balde): O(n) — por isso o rehash.
"""


class TabelaHash:
    _FATOR_CARGA_MAX = 0.75
    _CAPACIDADE_MIN = 8

    def __init__(self, capacidade_inicial=16):
        self._capacidade = max(self._CAPACIDADE_MIN, capacidade_inicial)
        self._baldes = [[] for _ in range(self._capacidade)]
        self._tamanho = 0

    # ------------------------------------------------------------ interno
    def _indice(self, chave):
        return hash(chave) % self._capacidade

    def _redimensionar(self, nova_capacidade):
        itens_antigos = self.itens()
        self._capacidade = nova_capacidade
        self._baldes = [[] for _ in range(self._capacidade)]
        self._tamanho = 0
        for chave, valor in itens_antigos:
            self.inserir(chave, valor)

    # ------------------------------------------------------------ público
    def inserir(self, chave, valor):
        """Insere ou atualiza o valor da chave."""
        balde = self._baldes[self._indice(chave)]
        for i, (k, _) in enumerate(balde):
            if k == chave:
                balde[i] = (chave, valor)
                return
        balde.append((chave, valor))
        self._tamanho += 1
        if self._tamanho / self._capacidade > self._FATOR_CARGA_MAX:
            self._redimensionar(self._capacidade * 2)

    def buscar(self, chave, padrao=None):
        """Retorna o valor da chave ou `padrao` se não existir."""
        for k, v in self._baldes[self._indice(chave)]:
            if k == chave:
                return v
        return padrao

    def contem(self, chave):
        for k, _ in self._baldes[self._indice(chave)]:
            if k == chave:
                return True
        return False

    def remover(self, chave):
        """Remove a chave. Retorna True se removeu, False se não existia."""
        balde = self._baldes[self._indice(chave)]
        for i, (k, _) in enumerate(balde):
            if k == chave:
                balde.pop(i)
                self._tamanho -= 1
                return True
        return False

    def limpar(self):
        self._capacidade = self._CAPACIDADE_MIN
        self._baldes = [[] for _ in range(self._capacidade)]
        self._tamanho = 0

    def itens(self):
        return [par for balde in self._baldes for par in balde]

    @property
    def fator_carga(self):
        return self._tamanho / self._capacidade

    @property
    def capacidade(self):
        return self._capacidade

    def __len__(self):
        return self._tamanho

    def __contains__(self, chave):
        return self.contem(chave)

    def __repr__(self):
        return "TabelaHash(itens={}, baldes={}, fator_carga={:.2f})".format(
            self._tamanho, self._capacidade, self.fator_carga)
