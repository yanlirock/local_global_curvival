import numpy
from scipy.integrate import trapz
from sklearn.utils import check_consistent_length, check_array

from nonparametric import CensoringDistributionEstimator, \
    SurvivalFunctionEstimator
from util import check_y_survival

__all__ = [
    'concordance_index_censored',
    'concordance_index_ipcw',
    'cumulative_dynamic_auc',
]


def _check_estimate(estimate, test_time):
    estimate = check_array(estimate, ensure_2d=False)
    if estimate.ndim != 1:
        raise ValueError(
            'Expected 1D array, got {:d}D array instead:\narray={}.\n'.format(
                estimate.ndim, estimate))
    check_consistent_length(test_time, estimate)
    return estimate


def _check_inputs(event_indicator, event_time, estimate):
    check_consistent_length(event_indicator, event_time, estimate)
    event_indicator = check_array(event_indicator, ensure_2d=False)
    event_time = check_array(event_time, ensure_2d=False)
    estimate = _check_estimate(estimate, event_time)

    if not numpy.issubdtype(event_indicator.dtype, numpy.bool_):
        raise ValueError(
            'only boolean arrays are supported as class labels for survival '
            'analysis, got {0}'.format(event_indicator.dtype))

    if len(event_time) < 2:
        raise ValueError("Need a minimum of two samples")

    if not event_indicator.any():
        raise ValueError("All samples are censored")

    return event_indicator, event_time, estimate


def _get_comparable(event_indicator, event_time, order):
    n_samples = len(event_time)
    tied_time = 0
    comparable = {}
    i = 0
    while i < n_samples - 1:
        time_i = event_time[order[i]]
        start = i + 1
        end = start
        while end < n_samples and event_time[order[end]] == time_i:
            end += 1

        # check for tied event times
        event_at_same_time = event_indicator[order[i:end]]
        censored_at_same_time = ~event_at_same_time
        for j in range(i, end):
            if event_indicator[order[j]]:
                mask = numpy.zeros(n_samples, dtype=bool)
                mask[end:] = True
                # an event is comparable to censored samples at same time point
                mask[i:end] = censored_at_same_time
                comparable[j] = mask
                tied_time += censored_at_same_time.sum()
        i = end

    return comparable, tied_time


def _estimate_concordance_index(event_indicator,
                                event_time,
                                estimate, weights, tied_tol=1e-8):
    order = numpy.argsort(event_time)

    comparable, tied_time = _get_comparable(event_indicator, event_time, order)

    concordant = 0
    discordant = 0
    tied_risk = 0
    numerator = 0.0
    denominator = 0.0
    for ind, mask in comparable.items():
        est_i = estimate[order[ind]]
        event_i = event_indicator[order[ind]]
        w_i = weights[order[ind]]

        est = estimate[order[mask]]

        assert (event_i,
                'got censored sample at index %d, but expected uncensored' %
                order[ind])

        ties = numpy.absolute(est - est_i) <= tied_tol
        n_ties = ties.sum()
        # an event should have a higher score
        con = est < est_i
        n_con = con[~ties].sum()

        numerator += w_i * n_con + 0.5 * w_i * n_ties
        denominator += w_i * mask.sum()

        tied_risk += n_ties
        concordant += n_con
        discordant += est.size - n_con - n_ties

    cindex = numerator / denominator
    return cindex, concordant, discordant, tied_risk, tied_time


def concordance_index_censored(event_indicator,
                               event_time, estimate,
                               tied_tol=1e-8):
    """Concordance index for right-censored data
    The concordance index is defined as the proportion of all comparable pairs
    in which the predictions and outcomes are concordant.
    Samples are comparable if for at least one of them an event occurred.
    If the estimated risk is larger for the sample with a higher time of
    event/censoring, the predictions of that pair are said to be concordant.
    If an event occurred for one sample and the other is known to be
    event-free at least until the time of event of the first, the second
    sample is assumed to *outlive* the first.
    When predicted risks are identical for a pair, 0.5 rather than 1 is added
    to the count of concordant pairs.
    A pair is not comparable if an event occurred for both of them at the same
    time or an event occurred for one of them but the time of censoring is
    smaller than the time of event of the first one.
    See [1]_ for further description.
    Parameters
    ----------
    event_indicator : array-like, shape = (n_samples,)
        Boolean array denotes whether an event occurred
    event_time : array-like, shape = (n_samples,)
        Array containing the time of an event or time of censoring
    estimate : array-like, shape = (n_samples,)
        Estimated risk of experiencing an event
    tied_tol : float, optional, default: 1e-8
        The tolerance value for considering ties.
        If the absolute difference between risk scores is smaller
        or equal than `tied_tol`, risk scores are considered tied.
    Returns
    -------
    cindex : float
        Concordance index
    concordant : int
        Number of concordant pairs
    discordant : int
        Number of discordant pairs
    tied_risk : int
        Number of pairs having tied estimated risks
    tied_time : int
        Number of comparable pairs sharing the same time
    References
    ----------
    [1] Harrell, F.E., Califf, R.M., Pryor, D.B., Lee, K.L., Rosati, R.A,
       "Multivariable prognostic models: issues in developing models,
       evaluating assumptions and adequacy, and measuring and reducing errors",
       Statistics in Medicine, 15(4), 361-87, 1996.
    """
    event_indicator, event_time, estimate = _check_inputs(
        event_indicator, event_time, estimate)

    w = numpy.ones_like(estimate)

    return _estimate_concordance_index(event_indicator, event_time, estimate,
                                       w, tied_tol)


def concordance_index_ipcw(survival_train,
                           survival_test,
                           estimate, tau=None, tied_tol=1e-8):
    """Concordance index for right-censored data based on inverse probability
    of censoring weights. This is an alternative to the estimator in :
    func:`concordance_index_censored` that does not depend on the distribution
    of censoring times in the test data. Therefore, the estimate is unbiased
    and consistent for a population concordance
    measure that is free of censoring.
    It is based on inverse probability of censoring weights, thus requires
    access to survival times from the training data to estimate the censoring
    distribution. Note that this requires that survival times `survival_test`
    lie within the range of survival times `survival_train`. This can be
    achieved by specifying the truncation time `tau`.
    The resulting `cindex` tells how well the given prediction model works in
    predicting events that occur in the time range from 0 to `tau`.
    The estimator uses the Kaplan-Meier estimator to estimate the
    censoring survivor function. Therefore, it is restricted to
    situations where the random censoring assumption holds and
    censoring is independent of the features.
    See [1]_ for further description.
    Parameters
    ----------
    survival_train : structured array, shape = (n_train_samples,)
        Survival times for training data to estimate the censoring
        distribution from.
        A structured array containing the binary event indicator
        as first field, and time of event or time of censoring as
        second field.
    survival_test : structured array, shape = (n_samples,)
        Survival times of test data.
        A structured array containing the binary event indicator
        as first field, and time of event or time of censoring as
        second field.
    estimate : array-like, shape = (n_samples,)
        Estimated risk of experiencing an event of test data.
    tau : float, optional
        Truncation time. The survival function for the underlying
        censoring time distribution :math:`D` needs to be positive
        at `tau`, i.e., `tau` should be chosen such that the
        probability of being censored after time `tau` is non-zero:
        :math:`P(D > \\tau) > 0`. If `None`, no truncation is performed.
    tied_tol : float, optional, default: 1e-8
        The tolerance value for considering ties.
        If the absolute difference between risk scores is smaller
        or equal than `tied_tol`, risk scores are considered tied.
    Returns
    -------
    cindex : float
        Concordance index
    concordant : int
        Number of concordant pairs
    discordant : int
        Number of discordant pairs
    tied_risk : int
        Number of pairs having tied estimated risks
    tied_time : int
        Number of comparable pairs sharing the same time
    References
    ----------
    [1] Uno, H., Cai, T., Pencina, M. J., D’Agostino, R. B., & Wei, L. J.
        "On the C-statistics for evaluating overall adequacy of risk prediction
        procedures with censored survival data".
        Statistics in Medicine, 30(10), 1105–1117.
    """
    test_event, test_time = check_y_survival(survival_test)

    if tau is not None:
        mask = test_time < tau
        survival_test = survival_test[mask]

    estimate = _check_estimate(estimate, test_time)

    cens = CensoringDistributionEstimator()
    cens.fit(survival_train)
    ipcw_test = cens.predict_ipcw(survival_test)
    if tau is None:
        ipcw = ipcw_test
    else:
        ipcw = numpy.empty(estimate.shape[0], dtype=ipcw_test.dtype)
        ipcw[mask] = ipcw_test
        ipcw[~mask] = 0

    w = numpy.square(ipcw)

    return _estimate_concordance_index(test_event, test_time, estimate, w,
                                       tied_tol)


def cumulative_dynamic_auc(survival_train, survival_test, estimate, times,
                           tied_tol=1e-8):
    """
    Estimator of cumulative/dynamic AUC for right-censored time-to-event data.
    The receiver operating characteristic (ROC) curve and the area under the
    ROC curve (AUC) can be extended to survival data by defining
    sensitivity (true positive rate) and specificity (true negative rate)
    as time-dependent measures. *Cumulative cases* are all individuals that
    experienced an event prior to or at time :math:`t` (:math:`t_i \\leq t`),
    whereas *dynamic controls* are those with :math:`t_i > t`.
    The associated cumulative/dynamic AUC quantifies how well a model can
    distinguish subjects who fail by a given time (:math:`t_i \\leq t`) from
    subjects who fail after this time (:math:`t_i > t`).
    Given an estimator of the :math:`i`-th individual's risk score
    :math:`\\hat{f}(\\mathbf{x}_i)`, the cumulative/dynamic AUC at time
    :math:`t` is defined as
    .. math::
        \\widehat{\\mathrm{AUC}}(t) =
        \\frac{\\sum_{i=1}^n \\sum_{j=1}^n I(y_j > t) I(y_i \\leq t) \\omega_i
        I(\\hat{f}(\\mathbf{x}_j) \\leq \\hat{f}(\\mathbf{x}_i))}
        {(\\sum_{i=1}^n I(y_i > t)) (\\sum_{i=1}^n I(y_i \\leq t) \\omega_i)}
    where :math:`\\omega_i` are inverse probability of censoring weights (IPCW)
    . To estimate IPCW, access to survival times from the training data is
    required to estimate the censoring distribution. Note that this requires
    that survival times `survival_test` lie within the range of survival times
    `survival_train`. This can be achieved by specifying `times` accordingly,
    e.g. by setting `times[-1]` slightly below the maximum expected follow-up
    time. IPCW are computed using the Kaplan-Meier estimator, which is
    restricted to situations where the random censoring assumption holds and
    censoring is independent of the features.
    The function also provides a single summary measure that refers to the mean
    of the :math:`\\mathrm{AUC}(t)` over the time range :math:
    `(\\tau_1, \\tau_2)`.
    .. math::
        \\overline{\\mathrm{AUC}}(\\tau_1, \\tau_2) =
        \\frac{1}{\\hat{S}(\\tau_1) - \\hat{S}(\\tau_2)}
        \\int_{\\tau_1}^{\\tau_2} \\widehat{\\mathrm{AUC}}(t)\\,d \\hat{S}(t)
    where :math:`\\hat{S}(t)` is the Kaplan–Meier estimator of the survival
    function. See [1]_, [2]_, [3]_ for further description.
    Parameters
    ----------
    survival_train : structured array, shape = (n_train_samples,)
        Survival times for training data to estimate the censoring
        distribution from.
        A structured array containing the binary event indicator
        as first field, and time of event or time of censoring as
        second field.
    survival_test : structured array, shape = (n_samples,)
        Survival times of test data.
        A structured array containing the binary event indicator
        as first field, and time of event or time of censoring as
        second field.
    estimate : array-like, shape = (n_samples,)
        Estimated risk of experiencing an event of test data.
    times : array-like, shape = (n_times,)
        The time points for which the area under the
        time-dependent ROC curve is computed. Values must be
        within the range of follow-up times of the test data
        `survival_test`.
    tied_tol : float, optional, default: 1e-8
        The tolerance value for considering ties.
        If the absolute difference between risk scores is smaller
        or equal than `tied_tol`, risk scores are considered tied.
    Returns
    -------
    auc : array, shape = (n_times,)
        The cumulative/dynamic AUC estimates (evaluated at `times`).
    mean_auc : float
        Summary measure referring to the mean cumulative/dynamic AUC
        over the specified time range `(times[0], times[-1])`.
    References
    ----------
    [1] H. Uno, T. Cai, L. Tian, and L. J. Wei, "Evaluating prediction rules
        for t-year survivors with censored regression models,"  Journal of the
        American Statistical Association, vol. 102, pp. 527–537, 2007.
    [2] H. Hung and C. T. Chiang, "Estimation methods for time-dependent AUC
        models with survival data,"
        Canadian Journal of Statistics, vol. 38, no. 1, pp. 8–26, 2010.
    [3] J. Lambert and S. Chevret, "Summary measure of discrimination in
        survival models based on cumulative/dynamic time-dependent ROC curves,"
        Statistical Methods in Medical Research, 2014.
    """
    test_event, test_time = check_y_survival(survival_test)

    estimate = _check_estimate(estimate, test_time)

    times = check_array(numpy.atleast_1d(times),
                        ensure_2d=False, dtype=test_time.dtype)
    times = numpy.unique(times)

    if times.max() >= test_time.max() or times.min() < test_time.min():
        raise ValueError(
            'all times must be within follow-up time of test data: [{}; {}['.
                format(test_time.min(), test_time.max()))

    # sort by risk score (descending)
    o = numpy.argsort(-estimate)
    test_time = test_time[o]
    test_event = test_event[o]
    estimate = estimate[o]
    survival_test = survival_test[o]

    cens = CensoringDistributionEstimator()
    cens.fit(survival_train)
    ipcw = cens.predict_ipcw(survival_test)

    n_samples = test_time.shape[0]
    scores = numpy.empty(times.shape[0], dtype=float)
    for k, t in enumerate(times):
        is_case = (test_time <= t) & test_event
        is_control = test_time > t
        n_controls = is_control.sum()

        true_pos = []
        false_pos = []
        tp_value = 0.0
        fp_value = 0.0
        est_prev = numpy.infty

        for i in range(n_samples):
            est = estimate[i]
            if numpy.absolute(est - est_prev) > tied_tol:
                true_pos.append(tp_value)
                false_pos.append(fp_value)
                est_prev = est
            if is_case[i]:
                tp_value += ipcw[i]
            elif is_control[i]:
                fp_value += 1
        true_pos.append(tp_value)
        false_pos.append(fp_value)

        sens = numpy.array(true_pos) / ipcw[is_case].sum()
        fpr = numpy.array(false_pos) / n_controls
        scores[k] = trapz(sens, fpr)

    if times.shape[0] == 1:
        mean_auc = scores[0]
    else:
        surv = SurvivalFunctionEstimator()
        surv.fit(survival_test)
        s_times = surv.predict_proba(times)
        # compute integral of AUC over survival function
        d = -numpy.diff(numpy.concatenate(([1.0], s_times)))
        integral = (scores * d).sum()
        mean_auc = integral / (1.0 - s_times[-1])

    return scores, mean_auc
