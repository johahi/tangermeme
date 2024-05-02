# attribute.py
# Contact: Jacob Schreiber <jmschreiber91@gmail.com>

import torch
import torch.nn.functional as F

import warnings

from typing import cast
from tqdm import trange
from .ersatz import dinucleotide_shuffle


def hypothetical_attributions(multipliers, X, references):
	"""A function for aggregating contributions into hypothetical attributions.

	When handling categorical data, like one-hot encodings, the gradients
	returned by a method like DeepLIFT/SHAP may need to be modified because
	the choice of one character at a position explicitly means that the other
	characters are not there. So, one needs to account for each character change 
	actually being the addition of one character AND the subtraction of another 
	character. Basically, once you've calculated the multipliers, you need to 
	subtract out the contribution of the nucleotide actually present and then 
	add in the contribution of the nucleotide you are becomming.

	Each element in the tensor is considered an independent example 

	As an implementation note: to be compatible with Captum, each input must
	be a tuple of length 1 and the returned value will be a tuple of length 1.
	I know this sounds silly but it's the most convenient implementation choice 
	to make the function compatible across DeepLiftShap implementations.


	Parameters
	----------
	multipliers: tuple of one torch.tensor, shape=(n_baselines, 4, length)
		The multipliers/gradient calculated by a method like DeepLIFT/SHAP.
		These should include values for both the observed characters and the
		unobserved characters at each position

	X: tuple of one torch.tensor, shape=(n_baselines, 4, length)
		The one-hot encoded sequence being explained

	references: tuple of one torch.tensor, shape=(n_baselines, 4, length)
		The one-hot encoded reference sequences, usually a shuffled version
		of the corresponding sequence in X.


	Returns
	-------
	projected_contribs: tuple of one torch.tensor, shape=(1, 4, length)
		The attribution values for each nucleotide in the input.
	"""

	for val in multipliers, X, references:
		if not isinstance(val, tuple) or len(val) != 1:
			raise ValueError("All inputs must be one-element tuples.")

		if not isinstance(val[0], torch.Tensor):
			raise ValueError("The first element of each input must be a "
				"tensor.")

		if val[0].shape != multipliers[0].shape:
			raise ValueError("Shape of all tensors must match.") 


	projected_contribs = torch.zeros_like(references[0], dtype=X[0].dtype, 
		device=X[0].device)
	
	for i in range(X[0].shape[1]):
		hypothetical_input = torch.zeros_like(X[0], dtype=X[0].dtype, 
			device=X[0].device)
		hypothetical_input[:, i] = 1.0
		hypothetical_diffs = hypothetical_input - references[0]
		hypothetical_contribs = hypothetical_diffs * multipliers[0]

		projected_contribs[:, i] = torch.sum(hypothetical_contribs, dim=1)

	return (projected_contribs,)


class DeepLiftShap():
	"""A vectorized version of the DeepLIFT/SHAP algorithm from Captum.

	DeepLIFT/SHAP is an approach for assigning importance to each input
	feature in an example using principles from game theory. At a high level,
	Shapley values are the average marginal contribution of each feature to
	the prediction and DeepLIFT/SHAP approximates this value for neural
	networks.

	This algorithm is implemented as a class because it is based on the Captum 
	approach of assigning hooks to layers in a PyTorch module object, where the
	hooks modify the gradients to implement the rescale rule. This object 
	implementation is much simpler than the one in Captum and adds in two 
	features: first, the implementation is vectorized so one can accept multiple 
	references for each example and these references can be different across 
	examples and, second, it adds in automatic checks that the theoretical 
	properties of the algorithm hold. 

	IMPORTANT: This implementation is minimal and only supports linear
	operations, convolutions, and dense layers. It does not support any form of
	non-linear pooling operation and may not work on custom operations. I do 
	not know whether it works with transformers. A warning will be raised if 
	the layers are not supported or yield attributions that do not satisfy the
	theoretical properties. Use the _captum_deep_lift_shap function when unsure.


	Parameters
	----------
	model: torch.nn.Module
		A PyTorch model to use for making predictions. These models can take in
		any number of inputs and make any number of outputs. The additional
		inputs must be specified in the `args` parameter.

	additional_nonlinear_ops: dict or None, optional
		If additional nonlinear ops need to be added to the dictionary of
		operations that can be handled by DeepLIFT/SHAP, pass a dictionary here
		where the keys are class types and the values are the name of the
		function that handle that sort of class. Make sure that the signature
		matches those of `_nonlinear` and `_maxpool` above. This can also be
		used to overwrite the hard-coded operations by passing in a dictionary
		with overlapping key names. If None, do not add any additional 
		operations. Default is None.

	ignore_layers: tuple
		A tuple of layer objects that should be ignored when assigning hooks.
		This should be the activations used in the model. Default is
		(torch.nn.ReLU,).

	eps: float, optional
		An epsilon with which to threshold gradients to ensure that there
		isn't an explosion. Default is 1e-6.

	warning_threshold: float, optional
		A threshold on the convergence delta that will always raise a warning
		if the delta is larger than it. Normal deltas are in the range of
		1e-6 to 1e-8. Note that convergence deltas are calculated on the
		gradients prior to the attribution_func being applied to them. Default 
		is 0.001. 

	verbose: bool, optional
		Whether to print the convergence delta for each example that is
		explained, regardless of whether it surpasses the warning threshold.
		Default is False.
	"""

	def __init__(self, model, additional_nonlinear_ops=None, eps=1e-10, 
		warning_threshold=0.0001, verbose=False):
		self.model = model
		self.eps = eps
		self.warning_threshold = warning_threshold
		self.verbose = verbose

		self.handles = []

		self._NON_LINEAR_OPS = {
			torch.nn.ReLU: _nonlinear,
			torch.nn.ELU: _nonlinear,
			torch.nn.LeakyReLU: _nonlinear,
			torch.nn.Sigmoid: _nonlinear,
			torch.nn.Tanh: _nonlinear,
			torch.nn.Softplus: _nonlinear,
			torch.nn.MaxPool1d: _maxpool,
			torch.nn.MaxPool2d: _maxpool,
			torch.nn.Softmax: _softmax
		}

		if additional_nonlinear_ops is not None:
			for key, value in additional_nonlinear_ops.items():
				self._NON_LINEAR_OPS[key] = value


	def attribute(self, X, references, args=None, target=0):
		"""Run the attribution algorithm on a set of inputs and baselines.

		This method actually handles calculating the attribution values and
		checking to make sure that they follow the theoretical properties of
		attributions.


		Parameters
		----------
		X: torch.Tensor, shape=(n, len(alphabet), length)
			A tensor of examples to calculate attribution values for.

		references: torch.Tensor, shape=(n, n_baselines, len(alphabet), length)
			A tensor of baselines/references to calculates attributions with
			respect to. The first dimension corresponds to the sequences that
			attributions are calculated for, the second dimension corresponds
			to the number of baselines that are being used for that example,
			the the last two dimensions should match that of the inputs.

		args: tuple, optional
			A tuple of additional forward arguments to pass into the model.
			Even when there is only a single additional argument this must be
			provided as a tuple.

		target: int, optional
			The output of the model to calculate gradients/attributions for. 
			This will index the last dimension of the predictions. Default is 0.


		Returns
		-------
		attributions: torch.Tensor, shape=(n, len(alphabet), length)
			Attributions for each example averaged over all of the baselines
			provided.
		"""

		assert X.shape == references.shape

		try:
			# Apply hooks and set up inputs
			self.model.apply(self._register_hooks)
			X_ = torch.cat([X, references])

			# Calculate the gradients using the rescale rule
			with torch.autograd.set_grad_enabled(True):
				if args is not None:
					args = (torch.cat([arg, arg]) for arg in args)
					y = self.model(X_, *args)[:, target]
				else:
					y = self.model(X_)[:, target]

				gradients = torch.autograd.grad(y.sum(), X)[0]

			# Check that the prediction-difference-from-reference is equal to
			# the sum of the attributions
			output_diff = torch.sub(*torch.chunk(y, 2))
			input_diff = torch.sum((X - references) * gradients, dim=(1, 2))
			convergence_deltas = abs(output_diff - input_diff)

			if torch.any(convergence_deltas > self.warning_threshold):
				warnings.warn("Convergence deltas too high: " +   
					str(convergence_deltas), RuntimeWarning)

			if self.verbose:
				print(convergence_deltas)

		finally:
			for handle in self.handles:
				handle.remove()

		return gradients

	def _fp_hook(self, module, inputs): 
		module.input = inputs[0].clone().detach()

	def _f_hook(self, module, inputs, outputs):
		module.output = outputs.clone().detach()

	def _b_hook(self, module, grad_input, grad_output):
		return self._NON_LINEAR_OPS[type(module)](module, grad_input, 
			grad_output, eps=self.eps)

	def _register_hooks(self, module): 
		if len(module._backward_hooks) > 0:
			return
		if not isinstance(module, tuple(self._NON_LINEAR_OPS.keys())):
			return

		self.handles.append(module.register_forward_hook(self._f_hook))
		self.handles.append(module.register_forward_pre_hook(self._fp_hook))
		self.handles.append(module.register_full_backward_hook(self._b_hook))


def _nonlinear(module, grad_input, grad_output, eps):
	"""An internal function implementing a general-purpose nonlinear correction.

	This function, copied and slightly modified from Captum, is meant to be
	the `rescale` rule applied to general non-linear functions such as
	activations.
	"""

	delta_in_ = torch.sub(*module.input.chunk(2))
	delta_out_ = torch.sub(*module.output.chunk(2))

	delta_in = torch.cat([delta_in_, delta_in_])
	delta_out = torch.cat([delta_out_, delta_out_])

	delta = delta_out / delta_in
	idxs = torch.abs(delta_in) < eps

	return (torch.where(idxs, grad_input[0], grad_output[0] * delta),)


def _softmax(module, grad_input, grad_output, eps):
	"""An internal function implementing a correction for softmax activations.

	This function, copied and slightly modified from Captum, is meant to be
	the `rescale` rule applied specifically to softmax activations without
	needing to remove them and operate on the underlying logits.
	"""

	delta_in_ = torch.sub(*module.input.chunk(2))
	delta_out_ = torch.sub(*module.output.chunk(2))

	delta_in = torch.cat([delta_in_, delta_in_])
	delta_out = torch.cat([delta_out_, delta_out_])

	delta = delta_out / delta_in
	idxs = torch.abs(delta_in) < eps

	grad_input_unnorm = torch.where(idxs, grad_input[0], grad_output[0] * delta)

	n = grad_input[0].numel()
	new_grad_inp = grad_input_unnorm - grad_input_unnorm.sum() * 1 / n
	return (new_grad_inp,)


def _maxpool(module, grad_input, grad_output, eps):
	"""An internal function implementing a 1D max-pooling correction.

	This function, copied and slightly modified from Captum, is meant to be
	the `rescale` rule applied to max pooling layers given their nature of
	aggregating values across multiple positions.
	"""

	if isinstance(module, torch.nn.MaxPool1d):
		pool_func, unpool_func = F.max_pool1d, F.max_unpool1d
	elif isinstance(module, torch.nn.MaxPool2d):
		pool_func, unpool_func = F.max_pool2d, F.max_unpool2d
	else:
		raise ValueError("module must be either MaxPool1d or MaxPool2d")


	with torch.no_grad():
		delta_in_ = torch.sub(*module.input.chunk(2))
		delta_in = torch.cat([delta_in_, delta_in_])

		output, output_ref = module.output.chunk(2)
		delta_out_xmax = torch.max(output, output_ref)
		delta_out = torch.cat([delta_out_xmax - output_ref, 
			output - delta_out_xmax])

		_, indices = pool_func(module.input, module.kernel_size, module.stride, 
			module.padding, module.dilation, module.ceil_mode, True)

		unpool_ = unpool_func(grad_output[0] * delta_out, indices, 
			module.kernel_size, module.stride, module.padding, 
			list(module.input.shape))
		unpool_delta, unpool_ref_delta = torch.chunk(unpool_, 2)

	unpool_delta_ = unpool_delta + unpool_ref_delta
	unpool_delta = torch.cat([unpool_delta_, unpool_delta_])
	idxs = torch.abs(delta_in) < eps

	new_grad_inp = torch.where(idxs, grad_input[0], unpool_delta / delta_in)
	return (new_grad_inp,)


def deep_lift_shap(model, X, args=None, target=0,  batch_size=32,
	references=dinucleotide_shuffle, n_shuffles=20, return_references=False, 
	hypothetical=False, warning_threshold=0.0001, additional_nonlinear_ops=None,
	print_convergence_deltas=False, raw_outputs=False, device='cuda', 
	random_state=None, verbose=False):
	"""Calculate attributions for a set of sequences using DeepLIFT/SHAP.

	This function will calculate the DeepLIFT/SHAP attributions on a set of
	sequences given a model. These attributions have the additive property that
	the sum of the attributions is ~equal to the difference in prediction
	between the original sequence and the reference sequences.

	As an implementation note, the batch size refers to the number of
	example-reference pairs that are being run simultaneously. When the batch
	size is smaller than the number of references, multiple batches will be
	run per example and the attributions will only be averaged only the
	references after they have all been covered. You may want to do this if the
	model or examples are so large that only a few can fit in memory at a time.
	The result will be identical to if all examples could fit in memory and
	each batch contained all the references.

	Convergence deltas are calculated automatically for each example-reference
	pair. Theoretically, these should be zero, but may in practice just be a
	small number due to machine precision issues with non-linear models. If
	these deltas exceed a warning threshold, a non-terminating warning will be 
	issued to let you know that the deltas have been exceeded.

	NOTE: predictions MUST yield a `(batch_size, n_targets)` tensor, even if
	n_targets is 1. If your model yields something more complicated you must
	wrap the model in a small class that operates on the outputs in a manner
	that yields such a tensor, e.g., by slicing the output or summing along
	a relevant axis.


	Parameters
	----------
	model: torch.nn.Module
		A PyTorch model to use for making predictions. These models can take in
		any number of inputs and make any number of outputs. The additional
		inputs must be specified in the `args` parameter.

	X: torch.tensor, shape=(-1, len(alphabet), length)
		A set of one-hot encoded sequences to calculate attribution values
		for. 

	args: tuple or None, optional
		An optional set of additional arguments to pass into the model. If
		provided, each element in the tuple or list is one input to the model
		and the element must be formatted to be the same batch size as `X`. If
		None, no additional arguments are passed into the forward function.
		Default is None.

	target: int, optional
		The output of the model to calculate gradients/attributions for. This
		will index the last dimension of the predictions. Default is 0.

	batch_size: int, optional
		The number of sequence-reference pairs to pass through DeepLiftShap at
		a time. Importantly, this is not the number of elements in `X` that
		are processed simultaneously (alongside ALL their references) but the
		total number of `X`-`reference` pairs that are processed. This means
		that if you are in a memory-limited setting where you cannot process
		all references for even a single sequence simultaneously that the
		work is broken down into doing only a few references at a time. Default
		is 32.

	references: func or torch.Tensor, optional
		If a function is passed in, this function is applied to each sequence
		with the provided random state and number of shuffles. This function
		should serve to transform a sequence into some form of signal-null
		background, such as by shuffling it. If a torch.Tensor is passed in,
		that tensor must have shape `(len(X), n_shuffles, *X.shape[1:])`, in
		that for each sequence a number of shuffles are provided. Default is
		the function `dinucleotide_shuffle`. 

	n_shuffles: int, optional
		The number of shuffles to use if a function is given for `references`.
		If a torch.Tensor is provided, this number is ignored. Default is 20.

	return_references: bool, optional
		Whether to return the references that were generated during this
		process. Only use if `references` is not a torch.Tensor. Default is 
		False. 

	hypothetical: bool, optional
		Whether to return attributions for all possible characters at each
		position or only for the character that is actually at the sequence.
		Practically, whether to return the returned attributions from captum
		with the one-hot encoded sequence. Default is False.

	warning_threshold: float, optional
		A threshold on the convergence delta that will always raise a warning
		if the delta is larger than it. Normal deltas are in the range of
		1e-6 to 1e-8. Note that convergence deltas are calculated on the
		gradients prior to the aggr_func being applied to them. Default 
		is 0.001. 

	additional_nonlinear_ops: dict or None, optional
		If additional nonlinear ops need to be added to the dictionary of
		operations that can be handled by DeepLIFT/SHAP, pass a dictionary here
		where the keys are class types and the values are the name of the
		function that handle that sort of class. Make sure that the signature
		matches those of `_nonlinear` and `_maxpool` above. This can also be
		used to overwrite the hard-coded operations by passing in a dictionary
		with overlapping key names. If None, do not add any additional 
		operations. Default is None.

	print_convergence_deltas: bool, optional
		Whether to print the convergence deltas for each example when using
		DeepLiftShap. Default is False.

	raw_outputs: bool, optional
		Whether to return the raw outputs from the method -- in this case,
		the multipliers for each example-reference pair -- or the processed
		attribution values. Default is False.

	device: str or torch.device, optional
		The device to move the model and batches to when making predictions. If
		set to 'cuda' without a GPU, this function will crash and must be set
		to 'cpu'. Default is 'cuda'. 

	random_state: int or None or numpy.random.RandomState, optional
		The random seed to use to ensure determinism. If None, the
		process is not deterministic. Default is None. 

	verbose: bool, optional
		Whether to display a progress bar. Default is False.


	Returns
	-------
	attributions: torch.tensor
		If `raw_outputs=False` (default), the attribution values with shape
		equal to `X`. If `raw_outputs=True`, the multipliers for each example-
		reference pair with shape equal to `(X.shape[0], n_shuffles, X.shape[1],
		X.shape[2])`. 

	references: torch.tensor, optional
		The references used for each input sequence, with the shape
		(n_input_sequences, n_shuffles, 4, length). Only returned if
		`return_references = True`. 
	"""

	attributions, references_ = [], []
	model = model.to(device).eval()

	if isinstance(references, torch.Tensor):
		n_shuffles = references.shape[1]

	n = X.shape[0] * n_shuffles
	Xi, rj, attr_ = [], [], []
	z = 0

	for i in trange(n, disable=not verbose):
		Xi.append(i // n_shuffles)
		rj.append(i % n_shuffles)

		if len(Xi) == batch_size or i == (n-1):
			_X = X[Xi].cpu()
			_args = None if args is None else tuple([a[Xi].to(device) 
				for a in args])

			# Handle reference sequences while ensuring that the same seed is
			# used for each shuffle even if not all shuffles are done in the
			# same batch.
			if isinstance(references, torch.Tensor):
				_references = references[Xi, rj]
			else:
				if random_state is None:
					_references = references(_X, n=1)[:, 0]
				else:
					_references = torch.cat([references(_X[j:j+1], n=1, 
						random_state=random_state+rj[j])[:, 0] 
							for j in range(len(_X))])

			_X = _X.to(device).float().requires_grad_()
			_references = _references.to(device).float().requires_grad_()

			# Run DeepLiftShap
			multipliers = DeepLiftShap(model, 
				warning_threshold=warning_threshold, 
				additional_nonlinear_ops=additional_nonlinear_ops, 
				verbose=print_convergence_deltas).attribute(_X, _references, 
				args=_args, target=target)
			
			# If not returning the raw multipliers then apply the correction for
			# character encodings
			if raw_outputs == False:
				multipliers = hypothetical_attributions((multipliers,), (_X,), 
					(_references,))[0]

			# attr_ is a list where each element is a tensor for the multipliers
			# of one example so that we can chunk them together once all
			# references for an example are 
			attr_.extend(list(multipliers.cpu().detach()))

			# When all references for a sequence have been calculated, remove
			# that block from the list of example-reference attributions and
			# add it to the final attribution list, averaging across references
			# if providing the processed results.
			while len(attr_) >= n_shuffles:
				attr_chunk = torch.stack(attr_[:n_shuffles])

				if raw_outputs == False:
					attr_chunk = attr_chunk.mean(dim=0)
					if not hypothetical:
						attr_chunk *= X[z].cpu()

				attributions.append(attr_chunk)
				attr_ = attr_[n_shuffles:]
				z += 1

			if return_references:
				references_.extend(list(_references.cpu().detach()))

			Xi, rj = [], []

	attributions = torch.stack(attributions)
	
	if return_references:
		references_ = torch.cat(references_).reshape(X.shape[0], n_shuffles, 
			*X.shape[1:])
		return attributions, references_
	
	return attributions


def _captum_deep_lift_shap(model, X, args=None, target=0, batch_size=32,
	references=dinucleotide_shuffle, n_shuffles=20,  return_references=False, 
	hypothetical=False, device='cuda', random_state=None, verbose=False):
	"""Calculate attributions using DeepLift/Shap and a given model. 

	This function will calculate DeepLift/Shap attributions on a set of
	sequences. It assumes that the model returns "logits" in the first output,
	not softmax probabilities, and count predictions in the second output.
	It will create GC-matched negatives to use as a reference and proceed
	using the given batch size.

	This is an internal/debugging function that is mostly meant to be used to
	check for differences with the `deep_lift_shap` method.


	Parameters
	----------
	model: torch.nn.Module
		A PyTorch model to use for making predictions. These models can take in
		any number of inputs and make any number of outputs. The additional
		inputs must be specified in the `args` parameter.

	X: torch.tensor, shape=(-1, len(alphabet), length)
		A set of one-hot encoded sequences to calculate attribution values
		for. 

	args: tuple or None, optional
		An optional set of additional arguments to pass into the model. If
		provided, each element in the tuple or list is one input to the model
		and the element must be formatted to be the same batch size as `X`. If
		None, no additional arguments are passed into the forward function.
		Default is None.

	target: int, optional
		The output of the model to calculate gradients/attributions for. This
		will index the last dimension of the predictions. Default is 0.

	batch_size: int, optional
		The number of sequence-reference pairs to pass through DeepLiftShap at
		a time. Importantly, this is not the number of elements in `X` that
		are processed simultaneously (alongside ALL their references) but the
		total number of `X`-`reference` pairs that are processed. This means
		that if you are in a memory-limited setting where you cannot process
		all references for even a single sequence simultaneously that the
		work is broken down into doing only a few references at a time. Default
		is 32.

	references: func or torch.Tensor, optional
		If a function is passed in, this function is applied to each sequence
		with the provided random state and number of shuffles. This function
		should serve to transform a sequence into some form of signal-null
		background, such as by shuffling it. If a torch.Tensor is passed in,
		that tensor must have shape `(len(X), n_shuffles, *X.shape[1:])`, in
		that for each sequence a number of shuffles are provided. Default is
		the function `dinucleotide_shuffle`. 

	n_shuffles: int, optional
		The number of shuffles to use if a function is given for `references`.
		If a torch.Tensor is provided, this number is ignored. Default is 20.

	return_references: bool, optional
		Whether to return the references that were generated during this
		process. Only use if `references` is not a torch.Tensor. Default is 
		False. 

	hypothetical: bool, optional
		Whether to return attributions for all possible characters at each
		position or only for the character that is actually at the sequence.
		Practically, whether to return the returned attributions from captum
		with the one-hot encoded sequence. Default is False.

	device: str or torch.device, optional
		The device to move the model and batches to when making predictions. If
		set to 'cuda' without a GPU, this function will crash and must be set
		to 'cpu'. Default is 'cuda'. 

	random_state: int or None or numpy.random.RandomState, optional
		The random seed to use to ensure determinism. If None, the
		process is not deterministic. Default is None. 

	verbose: bool, optional
		Whether to display a progress bar. Default is False.


	Returns
	-------
	attributions: torch.tensor
		The attributions calculated for each input sequence, with the same
		shape as the input sequences.

	references: torch.tensor, optional
		The references used for each input sequence, with the shape
		(n_input_sequences, n_shuffles, 4, length). Only returned if
		`return_references = True`. 
	"""

	from captum.attr import DeepLiftShap as CaptumDeepLiftShap

	model = model.to(device).eval()

	attributions = []
	references_ = []
	with torch.no_grad():
		for i in trange(len(X), disable=not verbose):
			_X = X[i:i+1].to(device)
			_args = None if args is None else tuple([a[i:i+1].to(device) 
				for a in args])

			# Calculate references
			if isinstance(references, torch.Tensor):
				_references = references[i].to(device)
			else:
				_references = references(_X, n=n_shuffles, 
					random_state=random_state)[0].to(device)
						
			attr = CaptumDeepLiftShap(model).attribute(_X, _references, 
				target=target, additional_forward_args=_args, 
				custom_attribution_func=hypothetical_attributions)

			if not hypothetical:
				attr = (attr * _X)
			
			if return_references:
				references_.append(_reference.unsqueeze(0))

			attributions.append(attr.cpu())

	attributions = torch.cat(attributions)

	if return_references:
		return attributions, torch.cat(references_)
	return attributions
